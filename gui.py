"""
Handles GUI and distributes tasks to other files (houses main app thread)
"""
from types import TracebackType
import threading
import traceback
import time
import sys

import wx.lib.filebrowsebutton as filebrowse
import wx

from script_options import ConditionalOptionError
from script_options import ScriptOptions as so
from actions import *
import config
import common

version = 'v1.0.2'
window_title = f'SM64 Bruteforce GUI {version}'

# ── Colour palette (dark theme) ───────────────────────────────────────────────
BG          = wx.Colour(28,  30,  38)   # main background
BG2         = wx.Colour(36,  39,  50)   # card / section background
BG3         = wx.Colour(44,  48,  62)   # input field background
BORDER      = wx.Colour(60,  65,  85)   # subtle border
ACCENT      = wx.Colour(99, 155, 255)   # blue accent
ACCENT2     = wx.Colour(130, 80, 220)   # purple accent (button gradient start)
GREEN       = wx.Colour(80,  210, 130)  # improvement indicator
YELLOW      = wx.Colour(255, 200,  60)  # warm highlight
FG          = wx.Colour(220, 225, 240)  # main text
FG_DIM      = wx.Colour(130, 140, 165)  # dimmed label text
FG_VAL      = wx.Colour(180, 220, 255)  # output value text (active)
RED         = wx.Colour(220,  75,  75)  # stop button

TT_COND = (
    "Conditional Options syntax:\n"
    "  if <condition>: add <value>;\n"
    "  if <condition>: return <value>;\n\n"
    "Available variables: x, y, z, hspd, coins, fyaw, action\n"
    "Separate multiple statements with ';'\n\n"
    "Examples:\n"
    '  if action == "pushing door": add 500;\n'
    "  if hspd > 100: return 0;\n"
    "  if coins >= 5: add 200;"
)

class _LogRedirect:
    """Redirects stdout writes to a wx.TextCtrl log box with colour-coded lines."""

    # (substring_to_match, fg_colour, bold)
    _RULES = [
        ('New best ever',   wx.Colour(80,  210, 130), True),
        ('New best',        wx.Colour(130, 200, 100), False),
        ('Error',           wx.Colour(220,  75,  75), True),
        ('error',           wx.Colour(220,  75,  75), True),
        ('---',             wx.Colour(99,  155, 255), False),
        ('Configuration',   wx.Colour(99,  155, 255), False),
        ('Frame',           wx.Colour(180, 180, 220), False),
        ('X:',              wx.Colour(200, 215, 255), False),
    ]
    _DEFAULT_FG = wx.Colour(200, 210, 230)

    def __init__(self, ctrl: wx.TextCtrl):
        self._ctrl = ctrl
        self._buf  = ''

    def write(self, text: str):
        if not text:
            return
        self._buf += text
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            wx.CallAfter(self._append_line, line)

    def _append_line(self, line: str):
        ctrl = self._ctrl
        attr = wx.TextAttr()
        attr.SetBackgroundColour(ctrl.GetBackgroundColour())

        fg   = self._DEFAULT_FG
        bold = False
        for substr, colour, is_bold in self._RULES:
            if substr in line:
                fg   = colour
                bold = is_bold
                break

        attr.SetTextColour(fg)
        f = ctrl.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL)
        attr.SetFont(f)

        ctrl.SetDefaultStyle(attr)
        ctrl.AppendText(line + '\n')
        ctrl.ShowPosition(ctrl.GetLastPosition())

    def flush(self):
        if self._buf:
            wx.CallAfter(self._append_line, self._buf)
            self._buf = ''

# ── Helpers ───────────────────────────────────────────────────────────────────
def _label(parent, text, pos, bold=False, dim=False):
    """Create a styled static text label."""
    st = wx.StaticText(parent, label=text, pos=pos)
    colour = FG_DIM if dim else FG
    st.SetForegroundColour(colour)
    if bold:
        f = st.GetFont()
        f.SetWeight(wx.FONTWEIGHT_BOLD)
        st.SetFont(f)
    return st

def _input(parent, pos, size, value='', centre=True):
    """Create a styled input TextCtrl."""
    style = wx.TE_CENTRE if centre else wx.TE_LEFT
    tc = wx.TextCtrl(parent, value=value, size=size, pos=pos, style=style | wx.BORDER_NONE)
    tc.SetBackgroundColour(BG3)
    tc.SetForegroundColour(FG)
    return tc

def _output_field(parent, pos, size=(88, 22)):
    """Create a read-only styled output field."""
    tc = wx.TextCtrl(parent, size=size, pos=pos,
                     style=wx.TE_RICH | wx.TE_READONLY | wx.TE_CENTRE | wx.BORDER_NONE)
    tc.SetBackgroundColour(BG2)
    tc.SetForegroundColour(FG_DIM)
    return tc

def _section(parent, label, pos, size):
    """Draw a dark card section box with a coloured header strip."""
    # Background panel for the section
    panel = wx.Panel(parent, pos=pos, size=size)
    panel.SetBackgroundColour(BG2)

    # Top accent line
    line = wx.Panel(panel, pos=(0, 0), size=(size[0], 2))
    line.SetBackgroundColour(ACCENT)

    # Label
    lbl = wx.StaticText(panel, label=f'  {label}', pos=(4, 6))
    lbl.SetForegroundColour(ACCENT)
    f = lbl.GetFont()
    f.SetWeight(wx.FONTWEIGHT_BOLD)
    f.SetPointSize(f.GetPointSize() - 1)
    lbl.SetFont(f)

    return panel

# ── Main Window ───────────────────────────────────────────────────────────────
class MainFrame(wx.Frame):
    def __init__(
        self, parent, ID, title, pos=wx.DefaultPosition,
        size=wx.DefaultSize,
        # allow resizing so the user can expand the window if needed
        style=wx.DEFAULT_FRAME_STYLE
    ):
        wx.Frame.__init__(self, parent, ID, title, pos, size, style)
        self._prev_fitness = None

        # ── Root panel with dark background ──────────────────────────────────
        self.panel = wx.Panel(self, -1)
        self.panel.SetBackgroundColour(BG)

        self.W = 780   # total window width usable
        self.PAD = 10  # outer padding
        # keep local copies for legacy code references
        PAD = self.PAD
        W   = self.W

        # reposition bottom controls when window is resized
        self.Bind(wx.EVT_SIZE, self._OnResize)
        # initial positioning in case the runtime size differs from requested
        wx.CallAfter(self._OnResize)

        # ── CONFIG section ───────────────────────────────────────────────────
        # broaden config area to accommodate new options
        cfg = _section(self.panel, 'CONFIG', pos=(PAD, PAD), size=(W - PAD*2, 160))

        # File browsers — we reparent them onto cfg
        self.libsm64_browse = filebrowse.FileBrowseButton(
            cfg, labelText='libsm64 .dll:', pos=(8, 22), size=(390, -1), fileMask='*.dll')
        self.libsm64_browse.SetBackgroundColour(BG2)

        self.m64_browse = filebrowse.FileBrowseButton(
            cfg, labelText='.m64 file:      ', pos=(8, 50), size=(390, -1), fileMask='*.m64')
        self.m64_browse.SetBackgroundColour(BG2)

        _label(cfg, 'Start Frame', (410, 24), dim=True)
        self.start_frame_txtbox = _input(cfg, (495, 21), (52, 22))

        _label(cfg, 'End Frame', (410, 51), dim=True)
        self.end_frame_txtbox = _input(cfg, (495, 48), (52, 22))

        # temperature and related annealing inputs
        _label(cfg, 'Temperature', (562, 24), dim=True)
        self.temp_txtbox = _input(cfg, (645, 21), (44, 22))

        # additional tuning parameters
        _label(cfg, 'Max attempts', (562, 50), dim=True)
        self.max_attempts_txtbox = _input(cfg, (645, 47), (44, 22))
        _label(cfg, 'Max iters', (562, 76), dim=True)
        self.max_iters_txtbox = _input(cfg, (645, 73), (44, 22))
        _label(cfg, 'Decay', (562, 102), dim=True)
        self.temp_decay_txtbox = _input(cfg, (645, 99), (44, 22))

        self.regularization_checkbox = wx.CheckBox(cfg, label='Regularize', pos=(562, 128))
        self.regularization_checkbox.SetForegroundColour(FG)
        self.regularization_checkbox.SetBackgroundColour(BG2)

        self.exit_on_goal_checkbox = wx.CheckBox(cfg, label='Stop on goal', pos=(645, 128))
        self.exit_on_goal_checkbox.SetForegroundColour(FG)
        self.exit_on_goal_checkbox.SetBackgroundColour(BG2)

        # ── FITNESS OPTIONS section ──────────────────────────────────────────
        fit_y = self.PAD + 160 + 6
        fit = _section(self.panel, 'FITNESS OPTIONS', pos=(PAD, fit_y), size=(W - PAD*2, 120))

        # Column 1: X Y Z
        _label(fit, 'Target', (30, 22), dim=True)
        _label(fit, 'Weight', (118, 22), dim=True)

        for i, (lbl, attr) in enumerate([('X', 'x'), ('Y', 'y'), ('Z', 'z')]):
            row_y = 38 + i * 26
            _label(fit, lbl, (8, row_y + 3), bold=True)
            setattr(self, f'{attr}_txtbox',        _input(fit, (22,  row_y), (92, 22)))
            setattr(self, f'{attr}_weight_txtbox', _input(fit, (118, row_y), (36, 22), value='1'))

        # Column 2: HSpd Coins FYaw
        _label(fit, 'Target', (185, 22), dim=True)
        _label(fit, 'Weight', (285, 22), dim=True)

        for i, (lbl, attr, w) in enumerate([('HSpd', 'hspd', 92), ('Coins', 'coins', 52), ('FYaw', 'fyaw', 60)]):
            row_y = 38 + i * 26
            _label(fit, lbl, (162, row_y + 3), bold=True)
            setattr(self, f'{attr}_txtbox',        _input(fit, (200, row_y), (w, 22)))
            setattr(self, f'{attr}_weight_txtbox', _input(fit, (285, row_y), (36, 22), value='1'))

        # Action dropdown
        _label(fit, 'Action', (336, 41), dim=True)
        choices = list(actions.keys())
        choices.insert(0, '')
        self.actn_dropdown = wx.ComboBox(fit, size=(155, 22), pos=(336, 58), choices=choices)
        self.actn_dropdown.SetBackgroundColour(BG3)
        self.actn_dropdown.SetForegroundColour(FG)

        # Conditional Options
        cond_x = 500
        _label(fit, 'Conditional Options', (cond_x, 22), dim=True)
        help_btn = wx.Button(fit, label='?', pos=(W - PAD*2 - 22, 20), size=(18, 16))
        help_btn.SetBackgroundColour(BORDER)
        help_btn.SetForegroundColour(FG)
        help_btn.SetToolTip(TT_COND)
        self.Bind(wx.EVT_BUTTON, self._ShowCondHelp, help_btn)

        self.cond_opt_txtbox = wx.TextCtrl(
            fit, size=(W - PAD*2 - cond_x - 4, 76), pos=(cond_x, 38),
            style=wx.TE_MULTILINE | wx.BORDER_NONE
        )
        self.cond_opt_txtbox.SetBackgroundColour(BG3)
        self.cond_opt_txtbox.SetForegroundColour(FG)
        self.cond_opt_txtbox.SetFont(
            wx.Font(8, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.cond_opt_txtbox.SetToolTip(TT_COND)

        # ── OUTPUT section ───────────────────────────────────────────────────
        out_y = fit_y + 120 + 6
        # store for later repositioning
        self.out_y = out_y
        out = _section(self.panel, 'OUTPUT', pos=(PAD, out_y), size=(W - PAD*2, 112))

        # Three value columns
        val_font = wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)

        def out_row(parent, label, lpos, fpos, fsize=(100, 22)):
            _label(parent, label, lpos, dim=True)
            f = _output_field(parent, fpos, fsize)
            f.SetFont(val_font)
            return f

        # Col 1
        self.best_x_val    = out_row(out, 'X',     (8,  38), (36,  36))
        self.best_y_val    = out_row(out, 'Y',     (8,  64), (36,  62))
        self.best_z_val    = out_row(out, 'Z',     (8,  90), (36,  88))

        # Col 2
        self.best_hspd_val  = out_row(out, 'HSpd',  (150, 38), (190, 36))
        self.best_coins_val = out_row(out, 'Coins', (150, 64), (190, 62))
        self.best_fyaw_val  = out_row(out, 'FYaw',  (150, 90), (190, 88))

        # Col 3 — action + fitness (wider)
        self.best_actn_val = out_row(out, 'Best Action',  (310, 38), (390, 36), fsize=(200, 22))
        self.best_fitn_val = out_row(out, 'Best Fitness', (310, 64), (390, 62), fsize=(150, 22))

        # Copy button
        self.copy_best_btn = wx.Button(out, label='⬆  Copy to Target', pos=(390, 84), size=(148, 24))
        self.copy_best_btn.SetBackgroundColour(BORDER)
        self.copy_best_btn.SetForegroundColour(FG)
        self.copy_best_btn.SetToolTip('Copy best output values into the fitness target fields')
        self.Bind(wx.EVT_BUTTON, self._CopyBestToTarget, self.copy_best_btn)

        # ── LOG section ──────────────────────────────────────────────────────
        log_y = out_y + 112 + 6
        self.log_y = log_y
        log = _section(self.panel, 'LOG', pos=(PAD, log_y), size=(W - PAD*2, 140))

        self.log_txtbox = wx.TextCtrl(
            log, size=(W - PAD*2 - 6, 114), pos=(3, 24),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.HSCROLL | wx.BORDER_NONE
        )
        self.log_txtbox.SetBackgroundColour(wx.Colour(18, 20, 28))
        self.log_txtbox.SetForegroundColour(wx.Colour(200, 210, 230))
        self.log_txtbox.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self._stdout_redir = _LogRedirect(self.log_txtbox)
        sys.stdout = self._stdout_redir

        # ── Bottom bar: timer + button ────────────────────────────────────────
        bot_y = self.log_y + 140 + 8
        # ensure bottom bar stays inside the frame even if size changed
        max_y = self.GetClientSize().GetHeight() - 50
        if bot_y > max_y:
            bot_y = max_y
        # save base value for reposition
        self.bot_y = bot_y
        self.timer = wx.Timer(self)
        self.timer_text = wx.StaticText(self.panel, label='⏱  00:00:00', pos=(self.PAD + 4, bot_y + 10))
        self.timer_text.SetForegroundColour(FG_DIM)

        self.brute_button = wx.Button(
            self.panel, label='▶   BRUTEFORCE!',
            pos=(self.W//2 - 110, bot_y), size=(220, 42)
        )
        self.brute_button.SetBackgroundColour(ACCENT)
        self.brute_button.SetForegroundColour(wx.Colour(10, 10, 20))
        bf = self.brute_button.GetFont()
        bf.SetWeight(wx.FONTWEIGHT_BOLD)
        bf.SetPointSize(bf.GetPointSize() + 1)
        self.brute_button.SetFont(bf)

        # ── Hotkeys ───────────────────────────────────────────────────────────
        ID_BRUTEFORCE_HOTKEY = wx.NewIdRef()
        ID_UNFOCUS_HOTKEY    = wx.NewIdRef()
        accelerators = [wx.AcceleratorEntry() for _ in range(2)]
        accelerators[0].Set(wx.ACCEL_CTRL,   ord('B'),       ID_BRUTEFORCE_HOTKEY)
        accelerators[1].Set(wx.ACCEL_NORMAL, wx.WXK_ESCAPE,  ID_UNFOCUS_HOTKEY)
        self.SetAcceleratorTable(wx.AcceleratorTable(accelerators))

        # ── Bindings ──────────────────────────────────────────────────────────
        self.Bind(wx.EVT_BUTTON, self.StartStopBruteforce, self.brute_button)
        self.Bind(wx.EVT_MENU,   self.StartStopBruteforce, id=ID_BRUTEFORCE_HOTKEY)
        self.Bind(wx.EVT_MENU,   self.Unfocus,             id=ID_UNFOCUS_HOTKEY)
        self.Bind(common.EVT_UPDATE_OUTPUT, self.UpdateOutput)
        self.Bind(wx.EVT_TIMER,  self.UpdateTimer)
        self.Bind(common.EVT_WAFEL_ERROR,
                  lambda evt: self.DisplayErrorWindow(
                      evt.exception_type, evt.exception_value, evt.exception_traceback))
        sys.excepthook = self.DisplayErrorWindow
        sys.tracebacklimit = 0
        sys.stderr = open('SM64BruteforceGUI.error.log', 'w')
        self.Bind(wx.EVT_CLOSE, self.OnWindowClose)

        self.SetFocus()

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _ShowCondHelp(self, event):
        dlg = wx.MessageDialog(self, TT_COND, 'Conditional Options Help', wx.OK | wx.ICON_INFORMATION)
        dlg.ShowModal(); dlg.Destroy()

    def _OnResize(self, event=None):
        """Reposition timer and brute button when window size changes"""
        # re-calc bottom Y using stored log_y
        bot_y = self.log_y + 140 + 8
        max_y = self.GetClientSize().GetHeight() - 50
        if bot_y > max_y:
            bot_y = max_y
        # move controls
        self.timer_text.SetPosition((self.PAD + 4, bot_y + 10))
        self.brute_button.SetPosition((self.W//2 - 110, bot_y))
        if event is not None:
            event.Skip()

    def _CopyBestToTarget(self, event):
        for src, dst in [
            (self.best_x_val,    self.x_txtbox),
            (self.best_y_val,    self.y_txtbox),
            (self.best_z_val,    self.z_txtbox),
            (self.best_hspd_val, self.hspd_txtbox),
            (self.best_coins_val,self.coins_txtbox),
            (self.best_fyaw_val, self.fyaw_txtbox),
        ]:
            v = src.GetValue()
            if v: dst.SetValue(v)

    # Setter Functions
    def SetGame(self):
        """Set Game path from GUI"""
        libsm64 = self.libsm64_browse.GetValue()
        if libsm64 != '':
            so.set_game(libsm64)
        if common.print_to_stdout:
            print('Game:', so.get_option_val('game'))
        return not isinstance(so.get_option_val('game'), type(None))

    def SetRange(self):
        """Set frame range from GUI"""
        start_frame = self.start_frame_txtbox.GetValue()
        end_frame = self.end_frame_txtbox.GetValue()
        if start_frame != '' and end_frame != '':
            so.set_range(int(start_frame), int(end_frame))
        if common.print_to_stdout:
            print('Start Frame:', so.get_option_val('start_frame'), 'End Frame:', 
                so.get_option_val('end_frame'))
        return (not isinstance(so.get_option_val('start_frame'), type(None)) and not isinstance(so.get_option_val('end_frame'), type(None)))

    def SetM64(self):
        """Set M64 path from GUI"""
        m64 = self.m64_browse.GetValue()
        if m64 != '':
            so.set_input_m64(m64)
            so.set_output_m64(m64[:-4] + '.bruteforced.m64')
        if common.print_to_stdout:
            print('Input .m64:', so.get_option_val('input_m64'))
        return not isinstance(so.get_option_val('input_m64'), type(None))

    def SetTemp(self):
        """Set starting bruteforce temperature from GUI"""
        temp = self.temp_txtbox.GetValue()
        if temp != '':
            so.set_temp(float(temp))
        else:
            # Default annealing temperature
            so.set_temp(0.4)
            self.temp_txtbox.SetValue('0.4')
        # additional annealing parameters, optional
        max_att = self.max_attempts_txtbox.GetValue()
        if max_att != '':
            so.set_max_attempts(int(max_att))
        else:
            so.set_max_attempts(500000)
            self.max_attempts_txtbox.SetValue('500000')
        max_it = self.max_iters_txtbox.GetValue()
        if max_it != '':
            so.set_max_iters(int(max_it))
        else:
            so.set_max_iters(500000)
            self.max_iters_txtbox.SetValue('500000')
        decay = self.temp_decay_txtbox.GetValue()
        if decay != '':
            so.set_temp_decay(float(decay))
        else:
            so.set_temp_decay(0.995)
            self.temp_decay_txtbox.SetValue('0.995')
        exit_goal = self.exit_on_goal_checkbox.GetValue()
        so.set_exit_on_goal(exit_goal)
        if common.print_to_stdout:
            print('Starting Temperature:', so.get_option_val('temp'))
            print('Max attempts:', so.get_option_val('max_attempts'),
                  'Max iters:', so.get_option_val('max_iters'),
                  'Temp decay:', so.get_option_val('temp_decay'),
                  'Stop on goal:', so.get_option_val('exit_on_goal'))
        return True

    def SetRegularization(self):
        """Set bruteforce regularization option from GUI"""
        so.set_regularization(self.regularization_checkbox.GetValue())
        if common.print_to_stdout:
            print('Regularize Inputs:', self.regularization_checkbox.GetValue())
        return True

    def SetDesCoords(self):
        """Set coordinate fitness options from GUI""" 
        if self.x_txtbox.GetValue() != '':
            so.set_des_coords(des_x=float(self.x_txtbox.GetValue()))
            if self.x_weight_txtbox.GetValue() != '':
                so.set_option_weight('des_x', float(self.x_weight_txtbox.GetValue()))
            else:
                so.set_option_weight('des_x', 1.0)
                self.x_weight_txtbox.SetValue('1')
        if self.y_txtbox.GetValue() != '':
            so.set_des_coords(des_y=float(self.y_txtbox.GetValue()))
            if self.y_weight_txtbox.GetValue() != '':
                so.set_option_weight('des_y', float(self.y_weight_txtbox.GetValue()))
            else:
                so.set_option_weight('des_y', 1.0)
                self.y_weight_txtbox.SetValue('1')
        if self.z_txtbox.GetValue() != '':
            so.set_des_coords(des_z=float(self.z_txtbox.GetValue()))
            if self.z_weight_txtbox.GetValue() != '':
                so.set_option_weight('des_z', float(self.z_weight_txtbox.GetValue()))
            else:
                so.set_option_weight('des_z', 1.0)
                self.z_weight_txtbox.SetValue('1')
        if common.print_to_stdout:
            print('Des X:', str(so.get_option_val('des_x')), '(' + ('N/A', str(so.get_option_weight('des_x')))[so.get_option_weight('des_x') != ''] + ')', 
                  'Des Y:', str(so.get_option_val('des_y')), '(' + ('N/A', str(so.get_option_weight('des_y')))[so.get_option_weight('des_y') != ''] + ')',
                  'Des Z:', str(so.get_option_val('des_z')), '(' + ('N/A', str(so.get_option_weight('des_z')))[so.get_option_weight('des_z') != ''] + ')')
        if so.get_option_val('des_x') or so.get_option_val('des_y') or so.get_option_val('des_z'):
            return True # return (not isinstance(so.get_option_val('des_x'), type(None)) and not isinstance(so.get_option_val('des_y'), type(None)) and not isinstance(so.get_option_val('des_z'), type(None)))
        return False

    def SetDesHSpd(self):
        """Set goal horizontal speed from GUI"""
        hspd = self.hspd_txtbox.GetValue()
        if hspd != '':
            so.set_des_hspd(float(hspd))
            hspd_weight = self.hspd_weight_txtbox.GetValue()
            if hspd_weight != '':
                so.set_option_weight('des_hspd', float(hspd_weight))
            else:
                so.set_option_weight('des_hspd', 1.0)
                self.hspd_weight_txtbox.SetValue('1')
        if common.print_to_stdout:
            print('Des HSpd:', str(so.get_option_val('des_hspd')), '(' + ('N/A', str(so.get_option_weight('des_hspd')))[so.get_option_weight('des_hspd') != ''] + ')')
        if so.get_option_val('des_hspd'):
            return True # return not isinstance(so.get_option_val('des_hspd'), type(None))
        return False

    def SetDesCoins(self):
        """Set goal coin count from GUI"""
        coins = self.coins_txtbox.GetValue()
        if coins != '':
            so.set_des_coins(int(coins))
            coins_weight = self.coins_weight_txtbox.GetValue()
            if coins_weight != '':
                so.set_option_weight('des_coins', float(coins_weight))
            else:
                so.set_option_weight('des_coins', 1.0)
                self.coins_weight_txtbox.SetValue('1')
        if common.print_to_stdout:
            print('Des Coins:', str(so.get_option_val('des_coins')), '(' + ('N/A', str(so.get_option_weight('des_coins')))[so.get_option_weight('des_coins') != ''] + ')')
        if so.get_option_val('des_coins'):
            return True # return not isinstance(so.get_option_val('des_coins'), type(None))
        return False

    def SetDesFYaw(self):
        """Set goal facing yaw from GUI"""
        fyaw = self.fyaw_txtbox.GetValue()
        if fyaw != '':
            so.set_des_fyaw(int(fyaw))
            fyaw_weight = self.fyaw_weight_txtbox.GetValue()
            if fyaw_weight != '':
                so.set_option_weight('des_fyaw', float(fyaw_weight))
            else:
                so.set_option_weight('des_fyaw', 1.0)
                self.fyaw_weight_txtbox.SetValue('1')
        if common.print_to_stdout:
            print('Des FYaw:', str(so.get_option_val('des_fyaw')), '(' + ('N/A', str(so.get_option_weight('des_fyaw')))[so.get_option_weight('des_fyaw') != ''] + ')')
        if so.get_option_val('des_fyaw'):
            return True # return not isinstance(so.get_option_val('des_fyaw'), type(None))
        return False

    def SetDesActn(self):
        """Set goal action from GUI"""
        actn = self.actn_dropdown.GetValue()
        if actn != '':
            so.set_des_actn(actions[actn])
        if common.print_to_stdout:
            print('Des Action:', ('None', actn)[actn != ''])
        if so.get_option_val('des_actn'):
            return True
        return False

    def SetConditionalOptions(self):
        """Set conditional options from GUI"""
        opts = so.set_conditional_options(self.cond_opt_txtbox.GetValue())
        # Ensure that we're not treating ints as bools or vice versa
        if not (isinstance(opts, bool) and opts):
            if isinstance(opts, bool) and opts == False:
                raise ConditionalOptionError('Too many conditional options. Maximum allowed is 10.')
            elif isinstance(opts, list) and opts[0] <= 10:
                opts[1] = opts[1][0].strip("[]'") # can't put this inside the f-string :/
                raise ConditionalOptionError(f"Banned keyword '{opts[1]}' found in statement {opts[0]+1}.")  
            else:
                raise ConditionalOptionError('Invalid Input.')
        if common.print_to_stdout:
            print('Conditional Option(s):', so.get_option_val('cond_opts'))
        return True
    # End Setter Functions

    def StartStopBruteforce(self, event=None):
        """Run setter functions and start bruteforcing with specified (or default, if not specified) options, or stop if already running"""
        self.Unfocus(event)

        if common.bruteforcing:
            self.timer.Stop()
            self.brute_button.SetLabel('▶   BRUTEFORCE!')
            self.brute_button.SetBackgroundColour(ACCENT)
            self.brute_button.SetForegroundColour(wx.Colour(10, 10, 20))
            common.bruteforcing = False
            self.UpdateOutput()
        else:
            self.start_time = time.time()
            self.timer.Start(1000)
            self.brute_button.SetLabel('■   STOP')
            self.brute_button.SetBackgroundColour(RED)
            self.brute_button.SetForegroundColour(wx.Colour(255, 255, 255))
            common.bruteforcing = True

            if common.print_to_stdout:
                print('--- Configuration ---')
            # If any of these are empty, error
            if not self.SetGame()           \
            or not self.SetRange()          \
            or not self.SetM64()            \
            or not self.SetTemp()           \
            or not self.SetRegularization():
                raise ValueError('One or more configuration options not provided.')
            # If all of these are empty, error
            opts_empty_check = 0
            cond_opt_res = self.SetConditionalOptions()
            if not cond_opt_res:
                return
            else:
                opts_empty_check += 1
            opts_empty_check += 1 if not self.SetDesCoords() else 0
            opts_empty_check += 1 if not self.SetDesHSpd()   else 0
            opts_empty_check += 1 if not self.SetDesCoins()  else 0
            opts_empty_check += 1 if not self.SetDesFYaw()   else 0
            opts_empty_check += 1 if not self.SetDesActn()   else 0
            opts_empty_check += 1 if not cond_opt_res        else 0
            if opts_empty_check == 0:
                raise ValueError('At least one fitness option must be provided.')
            if common.print_to_stdout:
                print('---------------------')

            # Start bruteforcing
            from user_defined_script import Bruteforcer
            self.bruteforcer = Bruteforcer()
            self.brute_thread = threading.Thread(target=Bruteforcer.bruteforce, args=(self.bruteforcer, common.frame_queue,), daemon=True)
            self.brute_thread.start()

            self.UpdateOutput()

    def UpdateOutput(self, event=None):
        """Update bruteforcer output on GUI"""
        all_vals = (self.best_x_val, self.best_y_val, self.best_z_val,
                    self.best_hspd_val, self.best_coins_val, self.best_fyaw_val,
                    self.best_actn_val, self.best_fitn_val)

        if common.bruteforcing:
            for ctrl in all_vals:
                ctrl.SetForegroundColour(FG_VAL)

            if event is not None:
                vals = event.vals
                self.best_x_val.SetLabel(f'{vals.x:.5f}')
                self.best_y_val.SetLabel(f'{vals.y:.5f}')
                self.best_z_val.SetLabel(f'{vals.z:.5f}')
                self.best_hspd_val.SetLabel(f'{vals.hspd:.5f}')
                self.best_coins_val.SetLabel(f'{vals.coins}')
                self.best_fyaw_val.SetLabel(f'{vals.fyaw}')
                self.best_actn_val.SetLabel(f'{GetActionName(vals.actn)}')
                self.best_fitn_val.SetLabel(f'{vals.fitness:.5f}')

                # Highlight fitness green when it improves (lower is better)
                if self._prev_fitness is None or vals.fitness < self._prev_fitness:
                    self.best_fitn_val.SetForegroundColour(GREEN)
                    self._prev_fitness = vals.fitness
                else:
                    self.best_fitn_val.SetForegroundColour(FG_VAL)
        else:
            self.Refresh()
            self._prev_fitness = None
            for ctrl in all_vals:
                ctrl.SetForegroundColour(FG_DIM)

    def UpdateTimer(self, event):
        """Updates the time elapsed on the GUI"""
        time_elapsed = time.gmtime(int(time.time() - self.start_time))
        cur_time = time.strftime('%H:%M:%S', time_elapsed)
        self.timer_text.SetLabel(f'⏱  {cur_time}')
    
    # https://stackoverflow.com/a/59687640
    def DisplayErrorWindow(self, exception_type: type[BaseException], exception_value: BaseException, exception_traceback: TracebackType):
        """Displays Python, Wafel, and Conditional Option error messages in a popup window"""
        self.StartStopBruteforce()

        trace = traceback.format_exception(exception_type, exception_value, exception_traceback)
        error_type = str(exception_type)

        # Wafel Error
        if error_type == "<class 'wafel.WafelError'>":
            error_message = 'Wafel Error:\n\n'
            if 'file error' in str(exception_value):
                error_message = 'Cannot find specified libsm64 file.'
            else:
                error_message = 'Cannot find specified m64 file.'
        # Conditional Option Error and Python Error
        elif error_type == "<class 'TypeError'>":
            error_message = str(exception_value)
        else:
            if error_type == "<class 'script_options.ConditionalOptionError'>":
                error_message = 'Conditional Option Error:\n\n'
            else:
                error_message = 'Python Error:\n\n'
            for i in trace:
                error_message += i
        # Remove the error type from the error message to make
        # it as simple to understand as possible for users
        error_message = error_message.replace('script_options.ConditionalOptionError:', '')
        error_message = error_message.replace('wafel.wafelError:', '')

        dialog_box = wx.MessageDialog(self, error_message, window_title, wx.OK|wx.ICON_EXCLAMATION)
        dialog_box.ShowModal()
        dialog_box.Destroy()

    def Unfocus(self, event):
        """Unfocuses current menu item"""
        self.SetFocus()

    def OnWindowClose(self, event):
        """Saves current parameters and kills app"""
        config.SaveConfig(self)
        self.Destroy()
# End Main Window

if __name__ == '__main__':
    app = wx.App()
    win = MainFrame(None, -1, window_title, size=(800, 640))
    win.SetIcon(wx.Icon('img\\DorrieChamp.ico'))
    win.Show(True)
    config.LoadConfig(win)
    common.frame_queue.put(win)
    app.MainLoop()