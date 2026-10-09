#!/usr/bin/env python3
"""RETRO-CONTROLLER // desktop widget shell.

Wires the finished media layer (``media.py``) and the finished UI
(``web/index.html`` + ``web/app.js`` + ``web/style.css``) into a real
always-on-top frameless desktop widget:

* one pywebview window, 360x400 CSS px (viewport-compensated), positioned
  near the bottom-right of the primary screen and remembered across runs;
* a ``js_api`` bridge exposing every method the UI calls;
* manual Win32 dragging (no pywebview ``easy_drag``, which swallows clicks);
* a pystray tray icon whose menu can always recover the widget from
  click-through;
* a single-instance guard so a second launch exits instead of stacking a
  second widget;
* a startup preflight for the two Windows components the widget cannot run
  without (the WebView2 runtime and .NET Framework 4.8), reported in a dialog
  box as well as on stderr so the silent launcher cannot fail invisibly.

Launched by ``run.cmd`` (pythonw.exe) / ``run.vbs`` (startup folder).
Everything here is primary-monitor only.  Nothing in this file imports a
dependency that is not already listed in CONTRACT.md.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import itertools
import json
import os
import sys
import threading
import time
import traceback
import winreg

# ---------------------------------------------------------------------------
# debug diagnostics
# ---------------------------------------------------------------------------
# Best-effort failures (remembering the window position, and the artwork
# extraction in media.py) are deliberately non-fatal, but they must not be
# invisible: with --debug (run.cmd --debug) or RETRO_DEBUG set, print one line
# to stderr so a silent failure is diagnosable.  No logging framework.
DEBUG = bool(os.environ.get('RETRO_DEBUG'))


def _dbg(message: str) -> None:
    if not DEBUG:
        return
    try:
        print('[retro] %s' % message, file=sys.stderr, flush=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# paths / constants
# ---------------------------------------------------------------------------
PROJECT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(PROJECT, 'web', 'index.html')
# The window icon (taskbar button + Alt-Tab entry).  pywebview's WinForms
# backend borrows the icon of the host executable - pythonw.exe, i.e. the blank
# page with the Python logo - when no window icon is set, so the shipped
# multi-size .ico is handed to webview.start(icon=...) instead (see
# _window_icon).  It is an absolute path on purpose: pywebview resolves a
# relative one against the CURRENT WORKING DIRECTORY, and the icon has to
# resolve identically whether the widget runs from this repo or from the
# extracted portable bundle.  A missing file is not fatal - pywebview silently
# keeps its default - so the fallback is guarded rather than assumed.
ICON = os.path.join(PROJECT, 'assets', 'retro-controller.ico')

WINDOW_TITLE = 'RETRO-CONTROLLER'
VIEW_W, VIEW_H = 360, 400                   # design CSS viewport (startup size)
VIEW_MIN_W, VIEW_MIN_H = 260, 290           # user-resize clamp, CSS pixels
VIEW_MAX_W, VIEW_MAX_H = 900, 1000          # user-resize clamp, CSS pixels
MARGIN = 24                                 # gap to the work-area edge

# --- startup settle loop ---------------------------------------------------
# The CSS viewport is driven to the target size by reading
# window.innerWidth/innerHeight and resizing by the difference.  A single read
# can arrive before a resize has been applied (or before the page has laid
# out), so the loop only ever acts on a value that has been read the SAME way
# twice in a row, and it verifies the final viewport instead of trusting the
# last resize.
SETTLE_POLL = 0.02                          # s between viewport reads
SETTLE_READ_TIMEOUT = 0.6                   # s to wait for a resize to land and read stable
SETTLE_MAX_STEPS = 24                       # hard bound on resize steps
# A remembered size this close (CSS px) to the design size in BOTH axes is
# treated as settle noise rather than a deliberate resize.  The two transients
# ever seen on this machine were 379x404 (+19/+4) and 376x393 (+16/-7); a real
# user resize is a deliberate, much larger change.
SIZE_INTENT_TOL = 64
BACKGROUND_COLOR = '#06120A'                # --bg; opaque, so no light halo
SINGLETON_NAME = 'Local\\retro-controller-singleton'

_local_appdata = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
APP_DIR = os.path.join(_local_appdata, 'retro-controller')
POSITION_FILE = os.path.join(APP_DIR, 'position.json')

# --- startup preflight -----------------------------------------------------
# The two Windows components this widget cannot run without.  Both are read
# only: nothing is installed, changed or repaired here, ever.
#
# WebView2 (the Chromium engine that draws the UI) publishes its version under
# an EdgeUpdate client key - per-machine (the 32-bit view is where the
# evergreen installer writes it) and per-user (installed for one account).
# .NET Framework 4.8+ is identified by the Release DWORD of the v4 Full key;
# 528040 is the 4.8 release value (Windows 11 and updated Windows 10 have
# more).
WEBVIEW2_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
WEBVIEW2_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE,
     'SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\' + WEBVIEW2_GUID),
    (winreg.HKEY_LOCAL_MACHINE,
     'SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\' + WEBVIEW2_GUID),
    (winreg.HKEY_CURRENT_USER,
     'Software\\Microsoft\\EdgeUpdate\\Clients\\' + WEBVIEW2_GUID),
)
NETFX_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE,
     'SOFTWARE\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full'),
    (winreg.HKEY_LOCAL_MACHINE,
     'SOFTWARE\\WOW6432Node\\Microsoft\\NET Framework Setup\\NDP\\v4\\Full'),
)
NETFX_MIN_RELEASE = 528040                   # .NET Framework 4.8
WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'
NETFX_URL = 'https://dotnet.microsoft.com/download/dotnet-framework/net48'
EXIT_MISSING_PREREQ = 3                      # distinct from 1 (running) / 2 (crash)

# Test hooks for the preflight only - never needed to run the widget:
#   RETRO_PREFLIGHT_REG_ROOT=<path>  pretend HKLM/HKCU hold nothing under it
#                                  (e.g. SOFTWARE\__no_such_root__) so the
#                                  real "not installed" path can be shown
#                                  without uninstalling anything;
#   RETRO_PREFLIGHT_FAKE_MISSING=webview2|dotnet|both   force that branch;
#   RETRO_PREFLIGHT_NO_DIALOG=1      print the message but skip the MessageBoxW
#                                  (so an automated run cannot block on a modal).
PREFLIGHT_REG_ROOT = os.environ.get('RETRO_PREFLIGHT_REG_ROOT') or 'SOFTWARE'
PREFLIGHT_FAKE = (os.environ.get('RETRO_PREFLIGHT_FAKE_MISSING') or '').strip().lower()
PREFLIGHT_NO_DIALOG = bool(os.environ.get('RETRO_PREFLIGHT_NO_DIALOG'))

# ---------------------------------------------------------------------------
# win32
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

HWND_TOPMOST = wt.HWND(-1)
HWND_NOTOPMOST = wt.HWND(-2)

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020

SW_MINIMIZE = 6

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
SPI_GETWORKAREA = 0x0030
ERROR_ALREADY_EXISTS = 183

# long-typed accessors (styles are 32-bit, so SetWindowLongW is correct here)
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]


def init_dpi() -> str:
    """Per-monitor DPI awareness so window rects and the screen agree."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return 'per-monitor-v2'
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return 'system'
    except Exception:
        return 'none'


def work_area() -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    if r.right <= r.left or r.bottom <= r.top:
        return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return (r.left, r.top, r.right, r.bottom)


def message_box(text: str, title: str = WINDOW_TITLE) -> None:
    try:
        user32.MessageBoxW(None, text, title, 0x00000040 | 0x00010000)  # ICONINFO|TOPMOST
    except Exception:
        pass


def acquire_single_instance():
    """Named-mutex guard.  Returns the handle, or None if already running."""
    h = kernel32.CreateMutexW(None, False, SINGLETON_NAME)
    err = ctypes.get_last_error()
    if not h:
        return None
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return None
    return h


# ---------------------------------------------------------------------------
# prerequisite preflight
# ---------------------------------------------------------------------------
# The widget needs the Edge WebView2 runtime (it *is* the renderer: without it
# pywebview cannot create a window at all) and .NET Framework 4.8 (pywebview's
# window host is WinForms).  Under the silent .vbs launcher a hard startup
# failure is invisible - pythonw has no console and the launcher returns at
# once - so a missing component is reported in a message box as well as on
# stderr, with what to install and where from, and exits 3.
def _exc_name(exc) -> str:
    return '%s: %s' % (type(exc).__name__, exc)


def _preflight_path(path: str) -> str:
    """Apply the RETRO_PREFLIGHT_REG_ROOT test override (identity by default)."""
    if PREFLIGHT_REG_ROOT.upper() == 'SOFTWARE':
        return path
    if path.upper().startswith('SOFTWARE\\') or path.upper() == 'SOFTWARE':
        return PREFLIGHT_REG_ROOT + path[len('SOFTWARE'):]
    return path


def _reg_label(hive, path: str) -> str:
    """HKLM\\... / HKCU\\... - the hive matters when reading the same subkey."""
    name = {winreg.HKEY_LOCAL_MACHINE: 'HKLM',
            winreg.HKEY_CURRENT_USER: 'HKCU'}.get(hive, 'HKEY')
    return '%s\\%s' % (name, path)


def _preflight_override(which: str, ok: bool, detail: str):
    """Apply the RETRO_PREFLIGHT_FAKE_MISSING test override to a result.

    ``which`` is 'webview2' or 'dotnet'.  The override flips only the verdict -
    the registry is still read (and its raw value reported), so the test can
    prove the *message and exit code* of a machine that lacks a component
    without that machine being uninstalled, renamed or otherwise touched.
    """
    if PREFLIGHT_FAKE in (which, 'both'):
        return False, ('forced MISSING by RETRO_PREFLIGHT_FAKE_MISSING=%s '
                       '[registry said: %s]' % (PREFLIGHT_FAKE, detail))
    if PREFLIGHT_FAKE in ('webview2', 'dotnet') and PREFLIGHT_FAKE != which:
        return True, ('forced PRESENT by RETRO_PREFLIGHT_FAKE_MISSING=%s '
                      '[registry said: %s]' % (PREFLIGHT_FAKE, detail))
    return ok, detail


def check_webview2():
    """(ok, detail) for the Edge WebView2 runtime; detail is the raw value read."""
    tried = []
    ok, detail = False, ''
    for hive, path in WEBVIEW2_KEYS:
        p = _preflight_path(path)
        label = _reg_label(hive, p)
        try:
            with winreg.OpenKey(hive, p, 0, winreg.KEY_READ) as key:
                try:
                    pv, _kind = winreg.QueryValueEx(key, 'pv')
                except OSError:
                    pv = None
        except OSError as exc:
            tried.append('%s -> %s' % (label, _exc_name(exc)))
            continue
        version = str(pv).strip() if pv is not None else ''
        if version and version != '0.0.0.0':
            ok, detail = True, '%s pv=%s' % (label, version)
            break
        tried.append('%s -> pv=%r' % (label, pv))
    if not ok:
        detail = '; '.join(tried) or 'no WebView2 client key found'
    return _preflight_override('webview2', ok, detail)


def check_dotnet():
    """(ok, detail) for .NET Framework 4.8+; detail is the raw Release value."""
    tried = []
    ok, detail = False, ''
    for hive, path in NETFX_KEYS:
        p = _preflight_path(path)
        label = _reg_label(hive, p)
        try:
            with winreg.OpenKey(hive, p, 0, winreg.KEY_READ) as key:
                try:
                    rel, _kind = winreg.QueryValueEx(key, 'Release')
                except OSError:
                    rel = None
                if rel is None:
                    try:
                        ver, _kind = winreg.QueryValueEx(key, 'Version')
                    except OSError:
                        ver = None
                    tried.append('%s -> Release absent, Version=%r' % (label, ver))
                    continue
        except OSError as exc:
            tried.append('%s -> %s' % (label, _exc_name(exc)))
            continue
        try:
            release = int(rel)
        except (TypeError, ValueError):
            tried.append('%s -> Release=%r (not a number)' % (label, rel))
            continue
        if release >= NETFX_MIN_RELEASE:
            ok, detail = True, '%s Release=%d' % (label, release)
            break
        tried.append('%s -> Release=%d (below %d)'
                     % (label, release, NETFX_MIN_RELEASE))
    if not ok:
        detail = '; '.join(tried) or 'no .NET Framework v4 Full key found'
    return _preflight_override('dotnet', ok, detail)


def preflight_results():
    """{'webview2': (ok, detail), 'dotnet': (ok, detail)} - read-only."""
    wv = check_webview2()
    net = check_dotnet()
    return {'webview2': wv, 'dotnet': net}


def preflight_message(results) -> str:
    """The text a stranger reads: what is missing, what to install, from where."""
    lines = ['RETRO-CONTROLLER cannot start: this machine is missing a Windows '
             'component the widget needs.', '']
    if not results['webview2'][0]:
        lines += [
            '* Microsoft Edge WebView2 Runtime - not found.',
            '  It is the engine that draws the widget window, so nothing can be',
            '  shown without it. Install the free "Evergreen Standalone',
            '  Installer" (x64) from:',
            '      ' + WEBVIEW2_URL,
            '',
        ]
    if not results['dotnet'][0]:
        lines += [
            '* .NET Framework 4.8 or newer - not found (needs Release %d or'
            % NETFX_MIN_RELEASE,
            '  higher; this machine reports none). Download the runtime from:',
            '      ' + NETFX_URL,
            '',
        ]
    lines += [
        'Install the missing piece(s) above, then start RETRO-CONTROLLER again.',
        'The portable build needs neither Python nor administrator rights -',
        'these two Windows components are the only things it expects to find',
        'on the machine already.',
        '',
        'Nothing was installed, changed or repaired by this program.',
    ]
    return '\n'.join(lines)


def run_preflight(report: bool = False, dialog: bool = True) -> int:
    """Check the two components.  0 when both are present, 3 when not.

    ``report`` prints the raw values even outside --debug (used by the
    --preflight flag); otherwise they only show up under --debug.  A failure is
    always printed to stderr and shown in a message box (unless the
    RETRO_PREFLIGHT_NO_DIALOG test override is set), because the silent launcher
    hides stderr completely.
    """
    results = preflight_results()
    for name, (ok, detail) in results.items():
        _dbg('preflight %s: %s (%s)'
             % (name, 'present' if ok else 'MISSING', detail))
        if report:
            print('[retro] preflight %s: %s  %s'
                  % (name, 'present' if ok else 'MISSING', detail), flush=True)
    if results['webview2'][0] and results['dotnet'][0]:
        return 0
    text = preflight_message(results)
    print(text, file=sys.stderr, flush=True)
    if dialog and not PREFLIGHT_NO_DIALOG:
        message_box(text)
    return EXIT_MISSING_PREREQ



# ---------------------------------------------------------------------------
# icon (a Pillow rendering of assets/crt-monitor-small.svg)
# ---------------------------------------------------------------------------
# The tray artwork is the 64-unit design of the shipped
# ``assets/crt-monitor-small.svg`` (and its rasterised references,
# ``assets/png/crt-monitor-small-*.png``): a case with a bevel, a recessed
# bezel and screen filled with a green phosphor glow, three green lines of
# "text", a power LED, a neck and a base - in the widget palette.  It is drawn
# programmatically, not loaded: no asset is read at runtime, so the portable
# bundle keeps working with no new file and no missing-file failure path.
#
# Every coordinate below is in the design's own 64-unit grid and scaled to the
# requested size.  The drawing is done on a supersampled canvas and reduced
# with a premultiplied LANCZOS downscale - that is what keeps the 16 px tray
# icon (where the design's 3-unit outline is under one physical pixel) from
# turning to mush, without any sub-pixel geometry ever being drawn directly.
_SVG_UNITS = 64                     # design grid (the SVG's viewBox)
_ICON_SS = 8                        # supersample factor

_ICON_CASE = (0x5A, 0x6A, 0x62, 255)        # #5A6A62  case
_ICON_CASE_DARK = (0x08, 0x11, 0x0D, 255)   # #08110D  dark case outline
_ICON_NECK = (0x46, 0x56, 0x4E, 255)        # #46564E  neck / panel
_ICON_BEZEL = (0x0A, 0x1B, 0x0F, 255)       # #0A1B0F  recessed bezel
_ICON_SCREEN = (0x06, 0x12, 0x0A, 255)      # #06120A  screen interior
_ICON_GREEN = (0x33, 0xFF, 0x66, 255)       # #33FF66  phosphor green
_ICON_BAR_A = (0x5A, 0xFF, 0x8C, 255)       # #5AFF8C  top text bar
_ICON_BAR_C = (0xA6, 0xFF, 0xB8, 255)       # #A6FFB8  bottom text bar
# the screen's radial phosphor glow: the (t, rgba) stops of the SVG's #sg
# gradient, which fills the screen rect (objectBoundingBox, r=0.85)
_ICON_GLOW = ((0.0, (0x33, 0xFF, 0x66, 0.50)),
              (0.6, (0x1E, 0x7A, 0x34, 0.25)),
              (1.0, (0x06, 0x12, 0x0A, 0.90)))


def _tray_icon_size() -> int:
    """Pixel size to render the tray artwork at.

    pystray serialises the image to a single-frame ICO and hands it to Win32
    ``LoadImage(..., LR_DEFAULTSIZE)``, which loads it at the ``SM_CXICON``
    system metric - measured on this machine, not assumed: a 16, 24, 48 or 64
    px source all come back as a 32x32 HICON, only a 32 px source is used
    natively.  Rendering at exactly that metric therefore means the artwork is
    never resampled on its way into the notification area; the shell then does
    the one clean downscale to the 16 px it paints at 100% scaling (and the
    metric grows with DPI, so the 24/32 px a scaled display shows are covered
    too).
    """
    sm_cxicon = 11
    try:
        n = int(user32.GetSystemMetrics(sm_cxicon))
    except Exception:
        n = 0
    return n if 16 <= n <= 256 else 32


def _screen_glow(w: int, h: int, u: float, box, mask_radius: int):
    """The screen's radial phosphor glow, as an RGBA layer ``w`` x ``h``.

    ``box`` is the screen's top-left in supersampled px and ``u`` the px per
    design unit.  The gradient is elliptical (the SVG's objectBoundingBox
    coords, centre 0.5/0.4, r 0.85) and is clipped to the screen's rounded
    rect, so it never bleeds onto the bezel.
    """
    from PIL import Image, ImageChops, ImageDraw
    gx, gy = box
    cx, cy = 11 + 0.50 * 42, 12 + 0.40 * 27      # #sg centre, design units
    rx, ry = 0.85 * 42, 0.85 * 27                # #sg radius, design units
    glow = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    px = glow.load()
    stops = _ICON_GLOW
    for j in range(h):
        dy = ((gy + j + 0.5) / u - cy) / ry
        for i in range(w):
            dx = ((gx + i + 0.5) / u - cx) / rx
            t = (dx * dx + dy * dy) ** 0.5
            if t > 1.0:
                t = 1.0
            for k in range(len(stops) - 1):
                t0, c0 = stops[k]
                t1, c1 = stops[k + 1]
                if t <= t1:
                    f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                    px[i, j] = (
                        int(round(c0[0] + (c1[0] - c0[0]) * f)),
                        int(round(c0[1] + (c1[1] - c0[1]) * f)),
                        int(round(c0[2] + (c1[2] - c0[2]) * f)),
                        int(round(255 * (c0[3] + (c1[3] - c0[3]) * f))))
                    break
    mask = Image.new('L', (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=mask_radius, fill=255)
    glow.putalpha(ImageChops.multiply(glow.getchannel('A'), mask))
    return glow


def _downscale(img, size: int):
    """LANCZOS downscale to ``size``, premultiplying alpha first.

    Resizing straight RGBA blends the colour channels against the (0,0,0,0)
    outside the silhouette and leaves a dark fringe on the antialiased edge;
    premultiplying, resizing and unpremultiplying keeps it clean.
    """
    from PIL import Image, ImageChops
    r, g, b, a = img.split()
    pre = Image.merge('RGBA', (ImageChops.multiply(r, a),
                               ImageChops.multiply(g, a),
                               ImageChops.multiply(b, a), a))
    pre = pre.resize((size, size), Image.LANCZOS)
    src = pre.load()
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    dst = out.load()
    for y in range(size):
        for x in range(size):
            rr, gg, bb, aa = src[x, y]
            if aa:
                dst[x, y] = (min(255, rr * 255 // aa), min(255, gg * 255 // aa),
                             min(255, bb * 255 // aa), aa)
    return out


def make_icon_image(size: int = 0):
    """The RETRO-CONTROLLER tray icon: an old CRT monitor, drawn at ``size`` px.

    ``size`` 0 (the default) means the size the notification area actually
    shows - see :func:`_tray_icon_size`.  Returns an RGBA ``PIL.Image``
    ``size`` x ``size``.  Geometry is scaled from the 64-unit design grid and
    rendered on a supersampled canvas, so it is crisp at 16 px and faithful at
    larger sizes alike.
    """
    from PIL import Image, ImageDraw
    size = max(8, min(256, int(size) or _tray_icon_size()))
    u = _ICON_SS * size / float(_SVG_UNITS)      # supersampled px per unit
    canvas = _ICON_SS * size

    def U(*units):
        """Design units -> supersampled px (rounded: crisp, no sub-pixel)."""
        return [int(round(v * u)) for v in units]

    def stroke(units):
        """A stroke width in design units -> integer supersampled px."""
        return max(1, int(round(units * u)))

    img = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def rrect(x0, y0, x1, y1, radius, fill=None, outline=None, width=0):
        """Rounded rect with an SVG-style stroke centred on its boundary
        (Pillow draws an outline inside the box, so the stroke box is grown
        by half the width and the radius with it)."""
        bx0, by0, bx1, by1 = U(x0, y0, x1, y1)
        r = int(round(radius * u))
        if fill is not None:
            d.rounded_rectangle([bx0, by0, bx1, by1], radius=r, fill=fill)
        if outline is not None and width > 0:
            hw = width / 2.0
            d.rounded_rectangle([bx0 - hw, by0 - hw, bx1 + hw, by1 + hw],
                                radius=r + hw, outline=outline, width=width)

    # neck + base first (the case overlaps the neck), as in the SVG
    neck = U(26, 45, 38, 45, 42, 53, 22, 53)
    d.polygon(neck, fill=_ICON_NECK)
    d.line(list(neck) + neck[:2], fill=_ICON_CASE_DARK,
           width=stroke(3), joint='curve')
    rrect(16, 51, 48, 59, 3, _ICON_CASE, _ICON_CASE_DARK, stroke(3))
    # case, recessed bezel, screen interior
    rrect(3, 4, 61, 48, 8, _ICON_CASE, _ICON_CASE_DARK, stroke(3))
    rrect(8, 9, 56, 42, 5, _ICON_BEZEL, _ICON_CASE_DARK, stroke(2))
    rrect(11, 12, 53, 39, 3, _ICON_SCREEN)
    # the green phosphor glow over the screen, clipped to its rounded rect
    sx0, sy0, sx1, sy1 = U(11, 12, 53, 39)
    img.alpha_composite(
        _screen_glow(sx1 - sx0, sy1 - sy0, u, (sx0, sy0), int(round(3 * u))),
        (sx0, sy0))
    # three lines of phosphor "text", then the power LED
    rrect(15, 17, 37, 22, 2, _ICON_BAR_A)        # #5AFF8C
    rrect(15, 26, 47, 31, 2, _ICON_GREEN)        # #33FF66
    rrect(15, 33, 29, 38, 2, _ICON_BAR_C)        # #A6FFB8
    lx, ly = U(53, 45)
    lr = int(round(3 * u))
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr], fill=_ICON_GREEN)
    hw = stroke(2) / 2.0
    d.ellipse([lx - lr - hw, ly - lr - hw, lx + lr + hw, ly + lr + hw],
              outline=_ICON_CASE_DARK, width=stroke(2))
    return _downscale(img, size)


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------
class Widget:
    def __init__(self):
        self.win = None
        self.media = None
        self.api = None
        self.icon = None
        self._hwnd = 0
        self._lock = threading.RLock()
        self._quit = threading.Event()
        self._quitting = False
        self._pos_tmp_seq = itertools.count(1)
        self.always_on_top = True
        self.click_through = False
        self._want_view = (VIEW_W, VIEW_H)   # CSS viewport to settle at startup
        self._want_pos = None                # restored (x, y); None = use the default
        self._user_size = False              # size was committed by a real resize
        self._geometry_ready = threading.Event()   # startup geometry proven good
        self._rs = None                      # in-flight user resize state

    # -- native handle ------------------------------------------------------
    def hwnd(self) -> int:
        h = self._hwnd
        if h and user32.IsWindow(h):
            return h
        try:
            native = self.win.native
            h = int(native.Handle.ToInt32())
        except Exception:
            h = 0
        if not (h and user32.IsWindow(h)):
            try:
                h = int(user32.FindWindowW(None, WINDOW_TITLE) or 0)
            except Exception:
                h = 0
        self._hwnd = h
        return h

    # -- ui-thread marshalling ---------------------------------------------
    def _on_ui_thread(self, fn) -> bool:
        """Post ``fn`` to the WinForms UI thread.  True when posted."""
        try:
            native = self.win.native
            from System import Action
            native.BeginInvoke(Action(fn))
            return True
        except Exception:
            return False

    # -- drag ---------------------------------------------------------------
    def _do_drag(self) -> None:
        hwnd = self.hwnd()
        if not hwnd:
            return
        try:
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
        except Exception:
            pass

    def start_drag(self) -> bool:
        try:
            if not self.hwnd():
                return False
            if self._on_ui_thread(self._do_drag):
                return True
            self._do_drag()          # fallback: run inline
            return True
        except Exception:
            return False

    # -- user resize (bottom-right grip) ------------------------------------
    # The grip is JS-tracked: pointerdown calls begin_resize(), pointermove
    # calls resize_drag() and pointerup calls end_resize().  The geometry is
    # driven from the *OS cursor position* (GetCursorPos), not from the event's
    # client coordinates, so it is unaffected by the page's CSS zoom.  The
    # top-left stays pinned (bottom-right resize), and the window is a
    # frameless popup so GetWindowRect == the CSS viewport: no frame is drawn.
    def _clamped_size(self, w, h):
        w = max(VIEW_MIN_W, min(VIEW_MAX_W, int(round(float(w)))))
        h = max(VIEW_MIN_H, min(VIEW_MAX_H, int(round(float(h)))))
        return w, h

    def resize_to(self, width, height) -> bool:
        """Set the window to an exact physical size (== CSS viewport here)."""
        try:
            w, h = self._clamped_size(width, height)
        except Exception:
            return False
        with self._lock:
            try:
                hwnd = self.hwnd()
                if not hwnd:
                    return False
                user32.SetWindowPos(hwnd, None, 0, 0, w, h,
                                    SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
            except Exception:
                return False
        # an explicit resize request (the bridge, or the grip's end_resize) is a
        # deliberate size, so remember it as such rather than treating it as a
        # settle transient.
        self._user_size = True
        self.save_window_geometry(force=True)
        return True

    def begin_resize(self) -> bool:
        """Remember the window rect and the cursor's screen position."""
        with self._lock:
            try:
                hwnd = self.hwnd()
                if not hwnd:
                    return False
                r = wt.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    return False
                pt = wt.POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    return False
                self._rs = (hwnd, r.left, r.top,
                            r.right - r.left, r.bottom - r.top, pt.x, pt.y)
                return True
            except Exception:
                return False

    def resize_drag(self) -> bool:
        """Resize so the bottom-right corner tracks the cursor; top-left fixed."""
        with self._lock:
            rs = self._rs
            if not rs:
                return False
            try:
                hwnd, x, y, w0, h0, sx, sy = rs
                if not user32.IsWindow(hwnd):
                    return False
                pt = wt.POINT()
                if not user32.GetCursorPos(ctypes.byref(pt)):
                    return False
                w, h = self._clamped_size(w0 + (pt.x - sx), h0 + (pt.y - sy))
                user32.SetWindowPos(hwnd, None, x, y, w, h,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
                return True
            except Exception:
                return False

    def end_resize(self) -> bool:
        with self._lock:
            self._rs = None
        # the user chose this size by hand: that is intent, not a transient, so
        # remember it even if the settle loop has not run yet.
        self._user_size = True
        self.save_window_geometry(force=True)
        return True

    # -- window flags -------------------------------------------------------
    def get_window_flags(self) -> dict:
        state = {'always_on_top': bool(self.always_on_top),
                 'click_through': bool(self.click_through)}
        try:
            hwnd = self.hwnd()
            if hwnd:
                ex = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
                state['always_on_top'] = bool(ex & WS_EX_TOPMOST)
                state['click_through'] = bool(ex & WS_EX_TRANSPARENT)
                self.always_on_top = state['always_on_top']
                self.click_through = state['click_through']
        except Exception:
            pass
        return {'always_on_top': state['always_on_top'],
                'click_through': state['click_through']}

    def toggle_always_on_top(self) -> bool:
        with self._lock:
            try:
                hwnd = self.hwnd()
                if not hwnd:
                    return bool(self.always_on_top)
                ex = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
                want = not bool(ex & WS_EX_TOPMOST)
                user32.SetWindowPos(hwnd, HWND_TOPMOST if want else HWND_NOTOPMOST,
                                    0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                self.always_on_top = bool(want)
            except Exception:
                pass
            return bool(self.always_on_top)

    def toggle_click_through(self) -> bool:
        with self._lock:
            try:
                hwnd = self.hwnd()
                if not hwnd:
                    return bool(self.click_through)
                ex = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
                want = not bool(ex & WS_EX_TRANSPARENT)
                if want:
                    new = ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                else:
                    new = (ex & ~WS_EX_TRANSPARENT) & ~WS_EX_LAYERED
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new)
                if want:
                    # keep the layered window fully opaque: without this the
                    # window can render invisible (the "stuck" failure mode)
                    try:
                        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
                    except Exception:
                        pass
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                                    SWP_NOACTIVATE | SWP_FRAMECHANGED)
                self.click_through = bool(want)
            except Exception:
                pass
            return bool(self.click_through)

    # -- geometry (position + size) ----------------------------------------
    def _size_is_deliberate(self, w: int, h: int, user_size: bool) -> bool:
        """Is (w, h) a size the widget should re-settle on?

        Yes when a resize gesture committed it, or when it is clearly not a
        settle transient: a size within SIZE_INTENT_TOL of the design size in
        both axes is noise (the transients seen on this machine were 379x404
        and 376x393), and re-settling on it would make the corruption
        self-perpetuating.
        """
        if user_size:
            return True
        return (abs(w - VIEW_W) > SIZE_INTENT_TOL
                or abs(h - VIEW_H) > SIZE_INTENT_TOL)

    def _load_geometry(self):
        """Remembered {x,y,w,h,user_size} under %LOCALAPPDATA%, or None."""
        try:
            with open(POSITION_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            x, y = int(data['x']), int(data['y'])
            if not (-10000 <= x <= 20000 and -10000 <= y <= 20000):
                return None
            # older files carried only x/y: fall back to the design size
            w, h = self._clamped_size(data.get('w', VIEW_W), data.get('h', VIEW_H))
            user_size = bool(data.get('user_size'))
            if not self._size_is_deliberate(w, h, user_size):
                _dbg('restored size %dx%d was never chosen by a resize; '
                     'using the %dx%d design size' % (w, h, VIEW_W, VIEW_H))
                w, h = VIEW_W, VIEW_H
            return {'x': x, 'y': y, 'w': w, 'h': h, 'user_size': user_size}
        except Exception:
            return None

    def save_geometry(self, x: int, y: int, w: int, h: int) -> None:
        try:
            os.makedirs(APP_DIR, exist_ok=True)
            # Each write gets its own temp file: the 1 Hz geometry watcher and
            # reset_position() run on different threads, and a shared
            # position.json.tmp let one writer's partial file be moved into
            # place by the other, silently losing the remembered geometry.
            tmp = '%s.%d.tmp' % (POSITION_FILE, next(self._pos_tmp_seq))
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'x': int(x), 'y': int(y),
                           'w': int(w), 'h': int(h),
                           'user_size': bool(self._user_size)}, fh)
            os.replace(tmp, POSITION_FILE)
        except Exception as exc:
            _dbg('geometry save failed: %s: %s' % (type(exc).__name__, exc))

    def window_geometry(self):
        """Live (x, y, w, h) from GetWindowRect, or None."""
        hwnd = self.hwnd()
        if not hwnd:
            return None
        r = wt.RECT()
        try:
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return None
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            return None

    def save_window_geometry(self, force: bool = False) -> None:
        # Never let a half-settled startup geometry become the remembered one:
        # until the settle loop has verified the window, a read is still in
        # flight, and persisting it is how one wrong size becomes every later
        # launch's target.  `force` is for a geometry the user just committed
        # (the resize grip, a bridge resize, Reset position).
        if not force and not self._geometry_ready.is_set():
            _dbg('geometry not saved: startup geometry is not verified yet')
            return
        g = self.window_geometry()
        if g:
            self.save_geometry(*g)

    def _move_to(self, x: int, y: int) -> None:
        hwnd = self.hwnd()
        if not hwnd:
            return
        try:
            user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        except Exception:
            pass

    def default_position(self):
        """Bottom-right of the primary work area, using the live window size."""
        w, h = VIEW_W + 16, VIEW_H + 40
        hwnd = self.hwnd()
        if hwnd:
            r = wt.RECT()
            try:
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right > r.left and r.bottom > r.top:
                    w, h = r.right - r.left, r.bottom - r.top
            except Exception:
                pass
        l, t, rr, bb = work_area()
        x = rr - w - MARGIN
        y = bb - h - MARGIN
        return max(l, x), max(t, y)

    def reset_position(self) -> None:
        x, y = self.default_position()
        self._move_to(x, y)
        self.save_window_geometry(force=True)

    def _position_watcher(self) -> None:
        hwnd = self.hwnd()
        last = None      # geometry seen on the previous tick
        saved = None     # geometry already written to position.json
        while not self._quit.wait(1.0):
            try:
                # Nothing may be remembered before startup has been proven
                # settled: a read taken while the window is still being placed
                # is in flight, not a position the user chose.
                if not self._geometry_ready.is_set():
                    last = None
                    continue
                # A resize gesture is in flight; end_resize() commits the final
                # size itself, so the intermediate ones are skipped entirely.
                if self._rs is not None:
                    last = None
                    continue
                if not hwnd or not user32.IsWindow(hwnd):
                    hwnd = self.hwnd()
                if not hwnd:
                    continue
                r = wt.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    continue
                if r.left < -30000 or r.top < -30000:
                    continue
                geo = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                if geo != last:
                    # changed since the last tick -> still moving; wait for a
                    # tick where the geometry does not change any more.
                    last = geo
                    continue
                if geo == saved:
                    continue
                if self.window_geometry() != geo:
                    # it moved again between the two reads: still in flight
                    last = None
                    continue
                saved = geo
                self.save_geometry(*geo)
            except Exception as exc:
                _dbg('geometry watcher: %s: %s' % (type(exc).__name__, exc))

    # -- viewport settle ----------------------------------------------------
    # create_window(wxh) does not always yield a wxh CSS viewport (the form can
    # lose pixels and Windows can clamp the size), so at startup the viewport is
    # driven to the target.  Every read is confirmed by a second read, every
    # resize waits for the reported size to actually move, and the final
    # viewport is read back instead of assumed: a stale read must never be taken
    # for an achieved resize, and a non-target size must never be accepted as
    # settled.
    #
    # Units: the error is measured in CSS pixels (window.innerWidth) but applied
    # to the window size, which is whatever unit pywebview reports for this
    # window.  At devicePixelRatio 1 those are the same unit and the correction
    # is applied verbatim - the behaviour verified on a 100% display.  Where the
    # two are NOT the same unit (a window whose reported size is in device
    # pixels, i.e. the Chromium device scale factor and the window's DPI scale
    # disagree) the same pixel error is a bigger move in window units, by
    # exactly devicePixelRatio, and without that factor the loop only converges
    # asymptotically - measured: 5 steps at 1.5x instead of 2.  The device
    # pixel ratio is read in the same round trip as the viewport and reported in
    # every settle line under --debug.
    def _read_viewport(self):
        """(innerWidth, innerHeight, devicePixelRatio) or None.

        One round trip; the ratio is read with the size so a resize can never be
        applied to a stale ratio.
        """
        try:
            vp = json.loads(self.win.evaluate_js(
                'JSON.stringify([window.innerWidth, window.innerHeight,'
                ' window.devicePixelRatio])'))
            dpr = float(vp[2]) if len(vp) > 2 and vp[2] else 1.0
            if not (dpr > 0):
                dpr = 1.0
            out = (int(vp[0]), int(vp[1]), dpr)
        except Exception as exc:
            _dbg('viewport read failed: %s: %s' % (type(exc).__name__, exc))
            return None
        _dbg('settle read: viewport %dx%d dpr %g' % out)
        return out

    @staticmethod
    def _window_unit_scale(win_px, css_px, dpr) -> float:
        """Window-size units per CSS pixel for this window.

        1.0 - the historical, verified behaviour - unless the size pywebview
        reports for the window is in device pixels rather than the CSS pixels
        the viewport is measured in, in which case the CSS error has to be
        converted into window units by devicePixelRatio before it is applied.
        The two regimes are told apart from the values themselves (the reported
        window size against the viewport width read in the same step), so the
        correction can only ever be multiplied by a ratio the window really is
        showing; at dpr 1 the factor is 1.0 by construction, so a 100% display
        keeps the exact behaviour verified before this change.
        """
        try:
            unit = float(win_px) / float(css_px)
            ratio = float(dpr)
        except (TypeError, ValueError, ZeroDivisionError):
            return 1.0
        if ratio > 1.0 and unit > 1.05 and abs(unit - ratio) < 0.25:
            return ratio
        return 1.0

    def _stable_viewport(self, timeout, changed_from=None):
        """Read the viewport until the same value comes back twice in a row.

        With ``changed_from`` the value must also differ from it, which is what
        makes "the resize I just asked for has landed" waitable.  Returns the
        last value read (or None when no read ever worked).
        """
        deadline = time.monotonic() + timeout
        last = None
        while True:
            cur = self._read_viewport()
            if cur is not None:
                if (last is not None and cur[:2] == last[:2]
                        and (changed_from is None
                             or cur[:2] != changed_from[:2])):
                    return cur
                last = cur
            if time.monotonic() >= deadline:
                return last
            time.sleep(SETTLE_POLL)

    def _settle_viewport(self, want_w, want_h):
        """Drive the CSS viewport to exactly want_w x want_h, or give up.

        Returns the target size only when the viewport really reads it; None
        when it could not be reached, so a non-target size is never reported as
        settled.  The step count is bounded, a no-op resize stops the loop and a
        viewport that keeps coming back stops it too, so it can neither spin nor
        oscillate forever.
        """
        want = (int(want_w), int(want_h))
        vp = self._stable_viewport(SETTLE_READ_TIMEOUT)
        _dbg('settle: target %dx%d, first stable read %s'
             % (want[0], want[1], vp))
        if vp is None:
            return None
        repeats = {}
        for step in range(SETTLE_MAX_STEPS):
            if vp[:2] == want:
                _dbg('settle: viewport is the target %dx%d dpr %g (step %d)'
                     % (want[0], want[1], vp[2], step))
                return want
            try:
                cur_w, cur_h = int(self.win.width), int(self.win.height)
            except Exception as exc:
                _dbg('settle: window size read failed: %s: %s'
                     % (type(exc).__name__, exc))
                return None
            unit = self._window_unit_scale(cur_w, vp[0], vp[2])
            nw, nh = self._clamped_size(cur_w + int(round((want[0] - vp[0]) * unit)),
                                        cur_h + int(round((want[1] - vp[1]) * unit)))
            if (nw, nh) == (cur_w, cur_h):
                _dbg('settle[%d]: resize would be a no-op (%dx%d), viewport %dx%d '
                     'window %dx%d - stopping instead of spinning'
                     % (step, nw, nh, vp[0], vp[1], cur_w, cur_h))
                break
            _dbg('settle[%d]: read %dx%d want %dx%d window %dx%d dpr %g '
                 'unit-scale %g -> resize(%d,%d)'
                 % (step, vp[0], vp[1], want[0], want[1], cur_w, cur_h, vp[2],
                    unit, nw, nh))
            self.win.resize(nw, nh)
            nxt = self._stable_viewport(SETTLE_READ_TIMEOUT, changed_from=vp)
            if nxt is None:
                break
            key = nxt[:2]
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > 2:
                _dbg('settle: viewport keeps returning to %dx%d - stopping'
                     % key)
                break
            vp = nxt
        # verify the FINAL viewport; never assume the last resize worked
        final = self._stable_viewport(SETTLE_READ_TIMEOUT)
        _dbg('settle: final viewport %s, target %s' % (final, want))
        return want if (final is not None and final[:2] == want) else None

    def _assert_geometry(self, want_w, want_h):
        """Re-assert (position, size) once after settling, and verify it.

        A transient during startup must never be what gets remembered, so the
        window is put back where the remembered geometry says, the target size
        is re-applied, and the viewport is read back.  Returns True only when
        the viewport really is the target.
        """
        if self._want_pos is None:
            self.reset_position()          # nothing remembered: the default spot
        else:
            self._move_to(*self._want_pos)  # physical px, no DPI multiply
        vp = self._stable_viewport(SETTLE_READ_TIMEOUT)
        unit = 1.0
        if vp is not None:
            try:
                cur_w = int(self.win.width)
            except Exception:
                cur_w = vp[0]
            unit = self._window_unit_scale(cur_w, vp[0], vp[2])
        try:
            self.win.resize(int(round(want_w * unit)), int(round(want_h * unit)))
        except Exception as exc:
            _dbg('geometry re-assert resize failed: %s: %s'
                 % (type(exc).__name__, exc))
        vp = self._stable_viewport(SETTLE_READ_TIMEOUT)
        _dbg('geometry re-assert: want %dx%d at %s -> rect %s, viewport %s'
             % (want_w, want_h, self._want_pos, self.window_geometry(), vp))
        if vp is not None and vp[:2] == (int(want_w), int(want_h)):
            return True
        _dbg('geometry re-assert did not hold; re-running the settle loop once')
        return self._settle_viewport(want_w, want_h) is not None

    # -- lifecycle ----------------------------------------------------------
    def setup_window(self) -> None:
        import webview
        self.media = _make_media()
        self.api = Api(self)

        saved = self._load_geometry()
        if saved is None:
            # provisional corner so the very first frame is already near it
            l, t, rr, bb = work_area()
            x = max(l, rr - (VIEW_W + 16) - MARGIN)
            y = max(t, bb - (VIEW_H + 40) - MARGIN)
            want_w, want_h = VIEW_W, VIEW_H
            self._want_pos = None          # no remembered spot: use the default
            self._user_size = False
        else:
            x, y = saved['x'], saved['y']
            want_w, want_h = saved['w'], saved['h']
            self._want_pos = (x, y)        # re-asserted once after settling
            # _load_geometry already snapped a non-deliberate size back to the
            # design size; anything that survived is a size to remember.
            self._user_size = (bool(saved['user_size'])
                               or (want_w, want_h) != (VIEW_W, VIEW_H))
            _dbg('restored geometry %s -> target %dx%d at %d,%d'
                 % (saved, want_w, want_h, x, y))
        self._want_view = (want_w, want_h)

        self.win = webview.create_window(
            WINDOW_TITLE,
            url=_page_uri(),
            js_api=self.api,
            width=want_w, height=want_h,
            x=x, y=y,
            frameless=True, on_top=True, transparent=False,
            background_color=BACKGROUND_COLOR,
            resizable=False, easy_drag=False, shadow=False,
        )
        self.win.events.shown += self._on_shown
        self.win.events.closed += self._on_closed

    def _on_shown(self) -> None:
        # tray lives off the GUI main loop
        try:
            self.start_tray()
        except Exception:
            traceback.print_exc()

    def _on_closed(self) -> None:
        self._quitting = True
        self._quit.set()

    def job(self) -> None:
        """Runs on a pywebview worker thread once the GUI loop is up."""
        try:
            if not self.win.events.loaded.wait(30):
                print('[retro] page did not load within 30s', file=sys.stderr)
                self.force_quit()
                return

            # fact 1: create_window(wxh) does not always yield a wxh CSS
            # viewport (the form can lose pixels and Windows can clamp the
            # size), so drive it to the target: the remembered size, or the
            # 360x400 design size.  The loop only proves a viewport it has read
            # the same way twice and has verified at the end.
            want_w, want_h = self._want_view
            settled = self._settle_viewport(want_w, want_h)
            if settled is None:
                print('[retro] viewport never settled at %dx%d' % (want_w, want_h),
                      file=sys.stderr)
            else:
                print('[retro] viewport settled at %dx%d' % settled, flush=True)

            # tell the page to (re)compute its proportional CSS zoom now that
            # the physical size is final (it also recomputes on every resize)
            try:
                self.win.evaluate_js(
                    'window.__retroApplyZoom && window.__retroApplyZoom()')
            except Exception:
                pass

            # Re-assert the whole geometry once - exact physical placement
            # (which avoids the pywebview DPI multiply) plus the target size -
            # and verify it, so a transient during startup can never be what
            # gets remembered.  Only a verified geometry may be persisted.
            placed = self._assert_geometry(want_w, want_h)
            self.always_on_top = True

            if placed:
                self._geometry_ready.set()
                self.save_window_geometry()
            else:
                print('[retro] geometry not verified at %dx%d; leaving '
                      'position.json untouched' % (want_w, want_h),
                      file=sys.stderr)

            # keep the saved position fresh while the user drags
            threading.Thread(target=self._position_watcher, daemon=True).start()
            print('[retro] widget ready: %s' % WINDOW_TITLE, flush=True)
        except Exception:
            traceback.print_exc()
            self.force_quit()

    # -- tray ---------------------------------------------------------------
    def start_tray(self) -> None:
        import pystray
        self.icon = pystray.Icon(
            'retro-controller',
            make_icon_image(),
            'RETRO-CONTROLLER',
            pystray.Menu(
                pystray.MenuItem('Always on top',
                                 self._tray_always_on_top,
                                 checked=lambda item: bool(self.always_on_top)),
                pystray.MenuItem('Click-through',
                                 self._tray_click_through,
                                 checked=lambda item: bool(self.click_through)),
                pystray.MenuItem('Reset position', self._tray_reset),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Quit', self._tray_quit),
            ),
        )
        self.icon.run_detached()
        print('[retro] tray icon started', flush=True)

    def _tray_always_on_top(self, icon, item) -> None:
        self.toggle_always_on_top()

    def _tray_click_through(self, icon, item) -> None:
        self.toggle_click_through()

    def _tray_reset(self, icon, item) -> None:
        self.reset_position()

    def _tray_quit(self, icon, item) -> None:
        self.quit()

    # -- exit ---------------------------------------------------------------
    def force_quit(self) -> None:
        self._quit.set()
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._quit.set()
        self.save_window_geometry()          # remember the final size + position
        # Pause only if something is actively playing; already-paused stays put.
        try:
            if self.media is not None:
                self.media.pause_if_playing()
        except Exception:
            pass
        self.stop_tray()
        self.stop_media()
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

    def minimize(self) -> bool:
        """Minimize the frameless window.  Never raises."""
        try:
            if self.win is not None and hasattr(self.win, 'minimize'):
                self.win.minimize()
                return True
        except Exception:
            pass
        hwnd = self.hwnd()
        if not hwnd:
            return False
        try:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            return True
        except Exception:
            return False

    def stop_tray(self) -> None:
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        self.icon = None

    def stop_media(self) -> None:
        try:
            if self.media is not None:
                self.media.shutdown()
        except Exception:
            pass

    def cleanup(self) -> None:
        self.stop_tray()
        self.stop_media()


def _make_media():
    import media
    return media.MediaController()


def _window_icon():
    """Absolute path to assets/retro-controller.ico, or None.

    pywebview only honours ``icon`` when the file really exists (a false path
    falls back to pythonw.exe's icon with nothing raised), so the existence is
    checked here and None is returned otherwise - an unbuilt/trimmed checkout
    keeps working exactly as before instead of depending on the asset.
    """
    return ICON if os.path.isfile(ICON) else None


def _page_uri() -> str:
    from pathlib import Path
    # no flags: WebView2 percent-encodes a '?' in a file:// URL and the load
    # dies with ERR_FILE_NOT_FOUND, and the widget must not run in demo mode.
    return Path(PAGE).as_uri()


# ---------------------------------------------------------------------------
# js_api
# ---------------------------------------------------------------------------
class Api:
    """Exactly the surface web/app.js calls, delegating to one shared
    MediaController (media methods pass through unchanged)."""

    def __init__(self, widget: Widget):
        self._w = widget

    # media.py pass-through
    def get_state(self):
        return self._w.media.get_state()

    def get_art(self, art_key):
        return self._w.media.get_art(art_key)

    def play_pause(self):
        return self._w.media.play_pause()

    def next_track(self):
        return self._w.media.next_track()

    def previous_track(self):
        return self._w.media.previous_track()

    def seek_fraction(self, fraction):
        return self._w.media.seek_fraction(fraction)

    def set_volume(self, level):
        return self._w.media.set_volume(level)

    def toggle_mute(self):
        return self._w.media.toggle_mute()

    def select_session(self, app_id):
        return self._w.media.select_session(app_id)

    # window shell
    def start_drag(self):
        return self._w.start_drag()

    def get_window_flags(self):
        return self._w.get_window_flags()

    def toggle_click_through(self):
        return self._w.toggle_click_through()

    def toggle_always_on_top(self):
        return self._w.toggle_always_on_top()

    # user resize (bottom-right grip)
    def begin_resize(self):
        return self._w.begin_resize()

    def resize_drag(self):
        return self._w.resize_drag()

    def end_resize(self):
        return self._w.end_resize()

    def resize_to(self, width, height):
        return self._w.resize_to(width, height)

    def quit_app(self):
        self._w.quit()
        return True

    def minimize_app(self):
        return bool(self._w.minimize())


# ---------------------------------------------------------------------------
def _enable_debug_channel(port: int) -> None:
    """Debug launch: expose the WebView2 DevTools protocol port so an outside
    process can inspect the live window, and keep the auto-opened DevTools
    window from covering the widget."""
    try:
        import webview
        webview.settings['REMOTE_DEBUGGING_PORT'] = int(port)
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
        print('[retro] debug: remote debugging on http://127.0.0.1:%d' % port, flush=True)
    except Exception:
        traceback.print_exc()


def main(argv=None) -> int:
    global DEBUG
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = '--debug' in argv
    DEBUG = bool(DEBUG or debug)          # --debug turns the one-line diagnostics on

    init_dpi()

    # Before any window exists: the two Windows components the widget cannot run
    # without.  Nothing is installed by this check - it reads two registry keys
    # and, when one is missing, prints what to install and shows the same text
    # in a message box (the silent .vbs launcher hides stderr completely) before
    # exiting 3.  --preflight is the diagnostic form: print the raw values,
    # never open a dialog, no window.
    preflight = run_preflight(report=('--preflight' in argv),
                              dialog=('--preflight' not in argv))
    if preflight != 0:
        return preflight
    if '--preflight' in argv:
        print('[retro] preflight: every required Windows component is present',
              flush=True)
        return 0

    if debug:
        try:
            port = int(os.environ.get('RETRO_DEBUG_PORT', '9222'))
        except Exception:
            port = 9222
        _enable_debug_channel(port)

    mutex = acquire_single_instance()
    if mutex is None:
        msg = 'RETRO-CONTROLLER is already running (look for its tray icon).'
        print('[retro] ' + msg, file=sys.stderr)
        if not debug:
            message_box(msg)
        return 1

    widget = Widget()
    try:
        import webview
        widget.setup_window()
        webview.start(widget.job, debug=debug, icon=_window_icon())
    except Exception:
        traceback.print_exc()
        widget.cleanup()
        return 2

    widget.cleanup()
    try:
        kernel32.CloseHandle(mutex)
    except Exception:
        pass
    print('[retro] exited cleanly', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
