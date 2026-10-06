#!/usr/bin/env python3
"""CRT-MEDIA // desktop widget shell.

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
  second widget.

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

# ---------------------------------------------------------------------------
# debug diagnostics
# ---------------------------------------------------------------------------
# Best-effort failures (remembering the window position, and the artwork
# extraction in media.py) are deliberately non-fatal, but they must not be
# invisible: with --debug (run.cmd --debug) or CRT_DEBUG set, print one line
# to stderr so a silent failure is diagnosable.  No logging framework.
DEBUG = bool(os.environ.get('CRT_DEBUG'))


def _dbg(message: str) -> None:
    if not DEBUG:
        return
    try:
        print('[crt] %s' % message, file=sys.stderr, flush=True)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# paths / constants
# ---------------------------------------------------------------------------
PROJECT = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(PROJECT, 'web', 'index.html')

WINDOW_TITLE = 'CRT-MEDIA'
VIEW_W, VIEW_H = 360, 400                   # design CSS viewport (startup size)
VIEW_MIN_W, VIEW_MIN_H = 260, 290           # user-resize clamp, CSS pixels
VIEW_MAX_W, VIEW_MAX_H = 900, 1000          # user-resize clamp, CSS pixels
MARGIN = 24                                 # gap to the work-area edge
BACKGROUND_COLOR = '#06120A'                # --bg; opaque, so no light halo
SINGLETON_NAME = 'Local\\crt-media-widget-singleton'

_local_appdata = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
APP_DIR = os.path.join(_local_appdata, 'crt-media-widget')
POSITION_FILE = os.path.join(APP_DIR, 'position.json')

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
# icon (pixel-art CRT in the fixed palette)
# ---------------------------------------------------------------------------
def make_icon_image(size: int = 64):
    from PIL import Image, ImageDraw
    bg = (6, 18, 10, 255)          # --bg
    dim = (30, 122, 52, 255)       # --dim
    base = (51, 255, 102, 255)     # --base
    bright = (166, 255, 184, 255)  # --bright
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 3, size - 3, size - 4], radius=9, fill=bg, outline=dim, width=2)
    # screen
    d.rounded_rectangle([9, 11, size - 10, size - 19], radius=4,
                        fill=(4, 12, 7, 255), outline=base, width=2)
    # play triangle
    d.polygon([(24, 21), (24, 41), (45, 31)], fill=base)
    # scanlines over the screen region
    for y in range(14, size - 20, 3):
        d.line([(11, y), (size - 12, y)], fill=(0, 0, 0, 70))
    # phosphor dot
    d.rectangle([13, 15, 15, 17], fill=bright)
    # stand
    d.rectangle([size // 2 - 7, size - 10, size // 2 + 7, size - 7], fill=dim)
    return img


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
                return True
            except Exception:
                return False

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
        self.save_window_geometry()
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
    def _load_geometry(self):
        """Remembered {x,y,w,h} under %LOCALAPPDATA%, or None when unusable."""
        try:
            with open(POSITION_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            x, y = int(data['x']), int(data['y'])
            if not (-10000 <= x <= 20000 and -10000 <= y <= 20000):
                return None
            # older files carried only x/y: fall back to the design size
            w = int(data.get('w', VIEW_W))
            h = int(data.get('h', VIEW_H))
            w = max(VIEW_MIN_W, min(VIEW_MAX_W, w))
            h = max(VIEW_MIN_H, min(VIEW_MAX_H, h))
            return {'x': x, 'y': y, 'w': w, 'h': h}
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
                           'w': int(w), 'h': int(h)}, fh)
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

    def save_window_geometry(self) -> None:
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
        self.save_window_geometry()

    def _position_watcher(self) -> None:
        hwnd = self.hwnd()
        last = None
        while not self._quit.wait(1.0):
            try:
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
                    last = geo
                    self.save_geometry(*geo)
            except Exception as exc:
                _dbg('geometry watcher: %s: %s' % (type(exc).__name__, exc))

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
        else:
            x, y = saved['x'], saved['y']
            want_w, want_h = saved['w'], saved['h']
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
                print('[crt] page did not load within 30s', file=sys.stderr)
                self.force_quit()
                return

            # fact 1: create_window(wxh) yields a smaller CSS viewport because
            # the form loses frame pixels; grow until it is exact.  The target
            # is the remembered size (or the 360x400 design size on first run).
            want_w, want_h = self._want_view
            settled = None
            vp = [want_w, want_h]
            for _ in range(40):
                vp = json.loads(self.win.evaluate_js(
                    'JSON.stringify([window.innerWidth, window.innerHeight])'))
                dx, dy = want_w - int(vp[0]), want_h - int(vp[1])
                if dx == 0 and dy == 0:
                    settled = (int(vp[0]), int(vp[1]))
                    break
                self.win.resize(self.win.width + dx, self.win.height + dy)
                time.sleep(0.05)
            if settled is None:
                print('[crt] viewport never settled at %dx%d (last %s)'
                      % (want_w, want_h, vp), file=sys.stderr)
            else:
                print('[crt] viewport settled at %dx%d' % settled, flush=True)

            # tell the page to (re)compute its proportional CSS zoom now that
            # the physical size is final (it also recomputes on every resize)
            try:
                self.win.evaluate_js(
                    'window.__crtApplyZoom && window.__crtApplyZoom()')
            except Exception:
                pass

            # exact physical placement (avoids the pywebview DPI multiply)
            if self._load_geometry() is None:
                self.reset_position()
            self.always_on_top = True

            # keep the saved position fresh while the user drags
            threading.Thread(target=self._position_watcher, daemon=True).start()
            print('[crt] widget ready: %s' % WINDOW_TITLE, flush=True)
        except Exception:
            traceback.print_exc()
            self.force_quit()

    # -- tray ---------------------------------------------------------------
    def start_tray(self) -> None:
        import pystray
        self.icon = pystray.Icon(
            'crt-media-widget',
            make_icon_image(),
            'CRT-MEDIA',
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
        print('[crt] tray icon started', flush=True)

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
        self.stop_tray()
        self.stop_media()
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

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


# ---------------------------------------------------------------------------
def _enable_debug_channel(port: int) -> None:
    """Debug launch: expose the WebView2 DevTools protocol port so an outside
    process can inspect the live window, and keep the auto-opened DevTools
    window from covering the widget."""
    try:
        import webview
        webview.settings['REMOTE_DEBUGGING_PORT'] = int(port)
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG'] = False
        print('[crt] debug: remote debugging on http://127.0.0.1:%d' % port, flush=True)
    except Exception:
        traceback.print_exc()


def main(argv=None) -> int:
    global DEBUG
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = '--debug' in argv
    DEBUG = bool(DEBUG or debug)          # --debug turns the one-line diagnostics on

    init_dpi()

    if debug:
        try:
            port = int(os.environ.get('CRT_DEBUG_PORT', '9222'))
        except Exception:
            port = 9222
        _enable_debug_channel(port)

    mutex = acquire_single_instance()
    if mutex is None:
        msg = 'CRT-MEDIA is already running (look for its tray icon).'
        print('[crt] ' + msg, file=sys.stderr)
        if not debug:
            message_box(msg)
        return 1

    widget = Widget()
    try:
        import webview
        widget.setup_window()
        webview.start(widget.job, debug=debug)
    except Exception:
        traceback.print_exc()
        widget.cleanup()
        return 2

    widget.cleanup()
    try:
        kernel32.CloseHandle(mutex)
    except Exception:
        pass
    print('[crt] exited cleanly', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
