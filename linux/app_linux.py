#!/usr/bin/env python3
"""CRT-MEDIA // Linux (GTK3 / WebKitGTK-4.1) desktop widget shell.

The Linux twin of the Windows ``app.py``.  It wires the finished media layer
(``media_linux.py``) and the finished UI (``web/index.html`` + ``web/app.js`` +
``web/style.css``) into a real always-on-top frameless desktop widget:

* one pywebview window on the GTK3 / WebKitGTK-4.1 backend, 360x400 CSS px,
  positioned near the bottom-right of the primary monitor and remembered
  across runs;
* a ``js_api`` bridge exposing the same method set the Windows twin exposes;
* manual dragging (no pywebview ``easy_drag``, which swallows clicks on the
  UI buttons): the last GTK button-press event is recorded and ``start_drag()``
  replays it into ``Gtk.Window.begin_move_drag``;
* a bottom-right grip resize driven by the real pointer position, clamped to
  the same 260x290 .. 900x1000 range the Windows build uses;
* a pystray tray icon (appindicator backend) whose menu can always recover the
  widget from click-through - and which refuses to enable click-through when
  no tray backend exists at all;
* a single-instance guard, a startup preflight for the things the widget
  cannot run without, and a top-level exception handler that writes a log and
  puts the failure on screen (a ``Terminal=false`` .desktop launch has no
  console to print to).

Everything in this module is import-safe: ``gi``, ``webview``, ``cairo``,
``dbus``, ``PIL`` and ``Xlib`` are all imported inside functions, so importing
this file on a machine without the GTK bindings (or on Windows, where the
contract check runs) cannot blow up.

Launched by ``run.sh`` (via the .desktop entry, the autostart entry, or by
hand).  Primary monitor only, same as the Windows build.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# debug diagnostics (same switch as the Windows twin: --debug or CRT_DEBUG)
# ---------------------------------------------------------------------------
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
PROJECT = os.path.dirname(os.path.abspath(__file__))          # .../linux
ROOT = os.path.dirname(PROJECT)                               # repo root
PAGE = os.path.join(ROOT, 'web', 'index.html')

WINDOW_TITLE = 'CRT-MEDIA'
APP_NAME = 'crt-media-widget'
VIEW_W, VIEW_H = 360, 400                   # design CSS viewport (startup size)
VIEW_MIN_W, VIEW_MIN_H = 260, 290           # user-resize clamp, CSS pixels
VIEW_MAX_W, VIEW_MAX_H = 900, 1000          # user-resize clamp, CSS pixels
MARGIN = 24                                 # gap to the work-area edge
BACKGROUND_COLOR = '#06120A'                # --bg; opaque, so no light halo

# A remembered size this close (CSS px) to the design size in BOTH axes is
# treated as settle noise rather than a deliberate resize, exactly as on
# Windows.  A real user resize is a deliberate, much larger change.
SIZE_INTENT_TOL = 64
SETTLE_POLL = 0.02
SETTLE_READ_TIMEOUT = 0.6
SETTLE_MAX_STEPS = 24

_state_home = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
STATE_DIR = os.path.join(_state_home, APP_NAME)
GEOMETRY_FILE = os.path.join(STATE_DIR, 'geometry.json')
LOG_FILE = os.path.join(STATE_DIR, 'widget.log')
LOCK_FILE = os.path.join(STATE_DIR, 'widget.lock')
TRAY_PNG = os.path.join(STATE_DIR, 'tray.png')

# exit codes
EXIT_OK = 0
EXIT_RUNNING = 1            # another instance already owns the lock
EXIT_CRASH = 2              # unexpected failure (logged, shown, exit 2)
EXIT_MISSING_PREREQ = 3     # a blocking preflight check failed

# The apt packages a Linux Mint 22 desktop is expected to already have (or
# that setup.sh offers to install).  Named here so the runtime can print the
# exact fix command when a binding is missing.
APT_WEBKIT = 'gir1.2-webkit2-4.1'
APT_GTK = 'gir1.2-gtk-3.0'
APT_GI = 'python3-gi'
APT_APPINDICATOR = 'gir1.2-ayatanaappindicator3-0.1'
APPINDICATOR_TYPELIB = 'AyatanaAppIndicator3'


# ---------------------------------------------------------------------------
# diagnostics with no console
# ---------------------------------------------------------------------------
def _log(message: str) -> None:
    """Append one line to ~/.local/state/crt-media-widget/widget.log."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), message))
    except Exception:
        pass


def _notify(text: str, title: str = WINDOW_TITLE) -> None:
    """Put a message on screen.  A .desktop launch has Terminal=false, so a
    crash is invisible without this; zenity and notify-send both ship with
    Linux Mint.  Both are fired; neither is allowed to block."""
    for cmd in (
        ['zenity', '--error', '--title', title, '--text', text, '--width', '520'],
        ['notify-send', '-u', 'critical', '-a', title, title, text],
    ):
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _session_type() -> str:
    return (os.environ.get('XDG_SESSION_TYPE') or '').strip().lower()


def is_wayland() -> bool:
    return _session_type() == 'wayland'


# ---------------------------------------------------------------------------
# lazy GTK imports
# ---------------------------------------------------------------------------
_GTK = {}


class MissingBindings(RuntimeError):
    """The apt GTK / WebKitGTK / gi bindings are not importable."""


def _load_gtk():
    """Import gi + Gtk 3.0 + Gdk 3.0 + WebKit2 4.1 + GLib, once.

    These come from apt (python3-gi, python3-gi-cairo, gir1.2-gtk-3.0,
    gir1.2-webkit2-4.1), not from pip, so they are imported lazily: importing
    this module must not fail on a machine that lacks them.
    """
    if _GTK:
        return _GTK
    try:
        import gi
    except Exception as exc:
        raise MissingBindings(
            'the Python gi bindings are not importable (%s). Install them with:\n'
            '  sudo apt install %s %s' % (exc, APT_GI, APT_GTK))
    try:
        gi.require_version('Gtk', '3.0')
        gi.require_version('Gdk', '3.0')
    except ValueError as exc:
        raise MissingBindings(
            'the GTK 3 typelib is not available (%s). Install it with:\n'
            '  sudo apt install %s' % (exc, APT_GTK))
    try:
        gi.require_version('WebKit2', '4.1')
    except ValueError as exc:
        raise MissingBindings(
            'the WebKitGTK 4.1 typelib is not available (%s). Install it with:\n'
            '  sudo apt install %s' % (exc, APT_WEBKIT))
    try:
        from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: F401
    except Exception as exc:
        raise MissingBindings(
            'GTK 3 / WebKitGTK 4.1 could not be loaded (%s). Install them with:\n'
            '  sudo apt install %s %s %s' % (exc, APT_GI, APT_GTK, APT_WEBKIT))
    _GTK.update(gi=gi, Gtk=Gtk, Gdk=Gdk, GLib=GLib, WebKit2=WebKit2)
    return _GTK


# ---------------------------------------------------------------------------
# preflight - before any window exists
# ---------------------------------------------------------------------------
def _check_display():
    """(ok, detail): is there a display to draw on at all?"""
    if os.environ.get('DISPLAY'):
        return True, 'DISPLAY=%s' % os.environ['DISPLAY']
    if os.environ.get('WAYLAND_DISPLAY'):
        return True, 'WAYLAND_DISPLAY=%s' % os.environ['WAYLAND_DISPLAY']
    return False, ('neither DISPLAY nor WAYLAND_DISPLAY is set - the widget '
                   'must be started from the desktop session, not from an SSH '
                   'shell or a cron job')


def _check_gtk():
    """(ok, detail): can gi import Gtk 3.0 + WebKit2 4.1?"""
    try:
        _load_gtk()
        return True, 'gi + Gtk 3.0 + Gdk 3.0 + WebKit2 4.1 import'
    except MissingBindings as exc:
        return False, str(exc)
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, exc)


def _check_dbus():
    """(ok, detail): is a session D-Bus bus reachable?  MPRIS lives on it."""
    addr = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    try:
        import dbus
    except Exception as exc:
        return False, ('the dbus Python module is not importable (%s). Install '
                       'it with: sudo apt install python3-dbus' % exc)
    try:
        bus = dbus.SessionBus()
        bus.list_names()
    except Exception as exc:
        return False, ('no session bus (%s); DBUS_SESSION_BUS_ADDRESS=%r. '
                       'Start the widget from the desktop session, not an SSH '
                       'shell' % (exc, addr))
    return True, 'session bus reachable (DBUS_SESSION_BUS_ADDRESS=%r)' % (addr,)


def _mpris_players():
    """Bus names of the MPRIS players visible right now (may be empty)."""
    try:
        import dbus
        bus = dbus.SessionBus()
        return [str(n) for n in bus.list_names()
                if str(n).startswith('org.mpris.MediaPlayer2.')]
    except Exception as exc:
        _dbg('MPRIS enumeration failed: %s: %s' % (type(exc).__name__, exc))
        return []


def _audio_backends():
    """Which system-volume backends exist on this machine."""
    found = []
    if shutil.which('wpctl'):
        found.append('wpctl')
    if shutil.which('pactl'):
        found.append('pactl')
    try:
        import importlib.util
        if importlib.util.find_spec('pulsectl') is not None:
            found.append('python3-pulsectl')
    except Exception:
        pass
    return found


def _tray_backend():
    """(ok, detail): can a tray icon backend be created at all?"""
    try:
        import importlib.util
        if importlib.util.find_spec('pystray') is None:
            return False, 'pystray is not installed in this environment'
    except Exception as exc:
        return False, 'pystray probe failed: %s' % exc
    try:
        gi = _GTK.get('gi') or __import__('gi')
        for name in ('AyatanaAppIndicator3', 'AppIndicator3'):
            try:
                gi.require_version(name, '0.1')
                __import__('gi.repository', fromlist=[name])
                return True, 'appindicator backend (%s 0.1)' % name
            except Exception:
                continue
    except Exception as exc:
        return False, ('the appindicator typelib is not available (%s). '
                       'Install it with: sudo apt install %s'
                       % (exc, APT_APPINDICATOR))
    return False, ('neither AyatanaAppIndicator3 nor AppIndicator3 is '
                   'available. Install it with: sudo apt install %s'
                   % APT_APPINDICATOR)


def _media_probe_line():
    """An optional one-line diagnostic from the media layer.  Purely
    informational: it can never make the preflight fail."""
    try:
        import media_linux
        probe = getattr(media_linux, 'probe', None)
        bus_ok = getattr(media_linux, 'bus_ok', None)
        vol = getattr(media_linux, 'volume_backend', None)
        bits = []
        if callable(bus_ok):
            try:
                ok, why = bus_ok()
                bits.append('bus_ok=%s (%s)' % (ok, why))
            except Exception as exc:
                bits.append('bus_ok raised %s' % type(exc).__name__)
        if callable(vol):
            try:
                bits.append('volume_backend=%s' % vol())
            except Exception as exc:
                bits.append('volume_backend raised %s' % type(exc).__name__)
        if callable(probe):
            try:
                bits.append('probe=%r' % (probe(),))
            except Exception as exc:
                bits.append('probe raised %s' % type(exc).__name__)
        return '; '.join(bits) or 'media_linux imported, no probe helpers'
    except Exception as exc:
        return 'media_linux not importable yet: %s: %s' % (type(exc).__name__, exc)


def preflight_results():
    """[(kind, name, ok, detail)] - 'block' facts and 'warn' facts."""
    results = []
    for name, fn in (('display present', _check_display),
                     ('GTK3 + WebKitGTK 4.1 import', _check_gtk),
                     ('D-Bus session bus reachable', _check_dbus)):
        try:
            ok, detail = fn()
        except Exception as exc:
            ok, detail = False, '%s: %s' % (type(exc).__name__, exc)
        results.append(('block', name, ok, detail))

    players = _mpris_players()
    if players:
        results.append(('warn', 'MPRIS players', True,
                        '%d visible: %s' % (len(players), ', '.join(players))))
    else:
        results.append(('warn', 'MPRIS players', False,
                        'none visible right now (normal when nothing is '
                        'playing - browsers publish MPRIS, Electron apps and '
                        'some players do not)'))

    backends = _audio_backends()
    results.append(('warn', 'audio backend', bool(backends),
                    ', '.join(backends) if backends else
                    'none of wpctl / pactl / python3-pulsectl found - the '
                    'volume slider has nothing to drive'))

    if is_wayland():
        results.append(('warn', 'session type', False,
                        'Wayland: always-on-top and click-through are '
                        'unavailable, and the window cannot be moved by the '
                        'widget (the compositor owns placement). Log out and '
                        'pick an X11 session for the full behaviour'))
    else:
        results.append(('warn', 'session type', True,
                        'X11 (%s) - always-on-top and click-through available'
                        % (_session_type() or '<unset>')))

    tray_ok, tray_detail = _tray_backend()
    results.append(('warn', 'tray backend', tray_ok,
                    tray_detail + ('' if tray_ok else
                                   ' - click-through will be refused, so the '
                                   'window can never become unclickable')))
    return results


def run_preflight(report=False, dialog=True) -> int:
    """Print every preflight result; return EXIT_OK or EXIT_MISSING_PREREQ.

    ``report`` prints every line even without --debug (used by --preflight).
    A blocking failure is printed AND shown in a dialog: under a Terminal=false
    .desktop launch stderr goes nowhere.
    """
    results = preflight_results()
    blocking_failures = []
    for kind, name, ok, detail in results:
        marker = 'ok  ' if ok else ('FAIL' if kind == 'block' else 'warn')
        line = '[crt] preflight %s %-28s %s' % (marker, name, detail)
        if kind == 'block' and not ok:
            blocking_failures.append((name, detail))
        if report or DEBUG or (kind == 'block' and not ok):
            print(line, flush=True)
        _log(line)

    if not blocking_failures:
        return EXIT_OK

    lines = ['CRT-MEDIA cannot start: a component the widget needs is missing '
             'or not reachable.', '']
    for name, detail in blocking_failures:
        lines.append('* %s' % name)
        for part in str(detail).splitlines():
            lines.append('    %s' % part)
        lines.append('')
    lines += ['The full report is in',
              '    ' + LOG_FILE,
              '',
              'Run the checker and paste its output if that does not explain it:',
              '    ./linux/selfcheck.sh']
    text = '\n'.join(lines)
    print(text, file=sys.stderr, flush=True)
    if dialog and not os.environ.get('CRT_PREFLIGHT_NO_DIALOG'):
        _notify(text)
    return EXIT_MISSING_PREREQ


# ---------------------------------------------------------------------------
# icon (pixel-art CRT in the fixed palette) - same drawing as the Windows twin
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
    d.rounded_rectangle([9, 11, size - 10, size - 19], radius=4,
                        fill=(4, 12, 7, 255), outline=base, width=2)
    d.polygon([(24, 21), (24, 41), (45, 31)], fill=base)
    for y in range(14, size - 20, 3):
        d.line([(11, y), (size - 12, y)], fill=(0, 0, 0, 70))
    d.rectangle([13, 15, 15, 17], fill=bright)
    d.rectangle([size // 2 - 7, size - 10, size // 2 + 7, size - 7], fill=dim)
    return img


# ---------------------------------------------------------------------------
# single instance
# ---------------------------------------------------------------------------
def acquire_single_instance():
    """flock guard on ~/.local/state/crt-media-widget/widget.lock.

    Returns the open file object (keep it alive), or None when another
    instance already holds the lock.
    """
    try:
        import fcntl          # POSIX-only, so imported here, not at module scope
    except Exception:
        return True           # cannot guard: better to run than to refuse
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        fh = open(LOCK_FILE, 'w')
    except Exception as exc:
        _dbg('lock file unavailable: %s: %s' % (type(exc).__name__, exc))
        return True
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    try:
        fh.write('%d\n' % os.getpid())
        fh.flush()
    except Exception:
        pass
    return fh


# ---------------------------------------------------------------------------
# widget
# ---------------------------------------------------------------------------
class Widget:
    def __init__(self):
        self.win = None
        self.media = None
        self.api = None
        self.icon = None                # pystray Icon
        self.indicator = None           # direct AyatanaAppIndicator3 fallback
        self.tray_ok = False            # a tray backend really exists
        self._lock = threading.RLock()
        self._quit = threading.Event()
        self._quitting = False
        self._pos_tmp_seq = 0
        self._press = None              # (button, x_root, y_root, time)
        self._gp = None                 # (x, y, w, h) remembered before resize
        self._rs = None                 # in-flight user resize state
        self._user_size = False
        self._geometry_ready = threading.Event()
        self._wayland = is_wayland()
        self._wayland_reported = False
        self._tray_failed = False
        self.always_on_top = not self._wayland
        self.click_through = False
        self._want_view = (VIEW_W, VIEW_H)
        self._want_pos = None           # restored (x, y); None = use the default

    # -- gtk access helpers -------------------------------------------------
    def _gtk_window(self):
        try:
            return self.win.native
        except Exception:
            return None

    def _on_gtk(self, fn, default=None):
        """Run ``fn`` on the GTK main thread and return its result.

        js_api calls already arrive on the GTK main thread; the tray runs in
        its own thread and must come back through GLib's idle queue.
        """
        GLib = _GTK.get('GLib')
        if GLib is None:
            try:
                GLib = _load_gtk()['GLib']
            except Exception:
                return default
        if GLib.MainContext.default().is_owner():
            try:
                return fn()
            except Exception as exc:
                _dbg('gtk call failed: %s: %s' % (type(exc).__name__, exc))
                return default
        box = {}
        done = threading.Event()

        def _run():
            try:
                box['r'] = fn()
            except Exception as exc:
                box['e'] = '%s: %s' % (type(exc).__name__, exc)
            finally:
                done.set()
            return False

        try:
            GLib.idle_add(_run)
        except Exception:
            return default
        if not done.wait(2.0):
            _dbg('gtk call timed out')
            return default
        if 'e' in box:
            _dbg('gtk call failed: %s' % box['e'])
            return default
        return box.get('r', default)

    # -- drag ---------------------------------------------------------------
    def _on_button_press(self, widget, event):
        """Record the last real button press.

        ``Gtk.Window.begin_move_drag`` needs an actual
        (button, root_x, root_y, timestamp) tuple and a JS bridge call cannot
        supply one, so the values are captured here and replayed by
        ``start_drag()``.  Returning False lets the press continue to the
        WebKit view, so the page's own buttons keep working.
        """
        try:
            self._press = (int(event.button), int(event.x_root),
                           int(event.y_root), int(event.time))
        except Exception:
            self._press = None
        return False

    def install_press_handler(self):
        """Attach the press recorder to the WebKit widget (main thread only)."""
        G = _load_gtk()
        widget = None
        try:
            from webview.platforms.gtk import BrowserView
            view = BrowserView.instances.get(getattr(self.win, 'uid', None))
            widget = getattr(view, 'webview', None)
        except Exception as exc:
            _dbg('BrowserView lookup failed: %s: %s' % (type(exc).__name__, exc))
        if widget is None:
            widget = self._gtk_window()
        if widget is None:
            return False
        try:
            widget.add_events(G['Gdk'].EventMask.BUTTON_PRESS_MASK)
        except Exception:
            pass
        try:
            widget.connect('button-press-event', self._on_button_press)
            return True
        except Exception as exc:
            _dbg('press handler failed: %s: %s' % (type(exc).__name__, exc))
            return False

    def _do_drag(self):
        w = self._gtk_window()
        if w is None or not self._press:
            return False
        button, rx, ry, ts = self._press
        if not button:
            return False
        try:
            w.begin_move_drag(int(button), int(rx), int(ry), int(ts))
            return True
        except Exception as exc:
            _dbg('begin_move_drag failed: %s: %s' % (type(exc).__name__, exc))
            return False

    def start_drag(self) -> bool:
        """Replay the recorded button press into a window move."""
        if self._wayland:
            self._report_wayland_once()
            return False
        if not self._press:
            return False
        return bool(self._on_gtk(self._do_drag, False))

    # -- geometry helpers ---------------------------------------------------
    def _clamped_size(self, w, h):
        w = max(VIEW_MIN_W, min(VIEW_MAX_W, int(round(float(w)))))
        h = max(VIEW_MIN_H, min(VIEW_MAX_H, int(round(float(h)))))
        return w, h

    def _workarea(self):
        G = _load_gtk()
        Gdk = G['Gdk']
        try:
            display = Gdk.Display.get_default()
            monitor = (display.get_primary_monitor()
                       or Gdk.Display.get_monitor(display, 0))
            if monitor is not None:
                r = monitor.get_workarea()
                if r.width > 0 and r.height > 0:
                    return (r.x, r.y, r.width, r.height)
        except Exception as exc:
            _dbg('workarea read failed: %s: %s' % (type(exc).__name__, exc))
        return (0, 0, 1920, 1080)

    def _pointer_pos(self):
        """Live screen pointer position: Gdk seat first, Xlib as a fallback."""
        try:
            Gdk = _load_gtk()['Gdk']
            display = Gdk.Display.get_default()
            seat = display.get_default_seat() if display is not None else None
            pointer = seat.get_pointer() if seat is not None else None
            if pointer is not None:
                pos = pointer.get_position()
                if isinstance(pos, tuple):
                    if len(pos) == 3:
                        return int(pos[1]), int(pos[2])
                    if len(pos) == 4:
                        return int(pos[2]), int(pos[3])
        except Exception as exc:
            _dbg('Gdk pointer read failed: %s: %s' % (type(exc).__name__, exc))
        if os.environ.get('DISPLAY'):
            try:
                from Xlib import display as xdisplay
                d = xdisplay.Display()
                q = d.screen().root.query_pointer()
                return int(q.root_x), int(q.root_y)
            except Exception as exc:
                _dbg('Xlib pointer read failed: %s: %s' % (type(exc).__name__, exc))
        return None

    def window_geometry(self):
        """Live (x, y, w, h) from GTK, or None."""
        def _read():
            w = self._gtk_window()
            if w is None:
                return None
            x, y = w.get_position()
            width, height = w.get_size()
            return (int(x), int(y), int(width), int(height))
        return self._on_gtk(_read, None)

    def resize_to(self, width, height) -> bool:
        """Set the window to an exact size (clamped), and remember it."""
        try:
            w, h = self._clamped_size(width, height)
        except Exception:
            return False
        ok = self._on_gtk(lambda: self._apply_size(w, h), False)
        if ok:
            self._user_size = True
            self.save_window_geometry(force=True)
        return bool(ok)

    def _apply_size(self, w, h):
        win = self._gtk_window()
        if win is None:
            return False
        win.resize(int(w), int(h))
        return True

    # -- user resize (bottom-right grip) ------------------------------------
    # The grip is JS-tracked: pointerdown calls begin_resize(), pointermove
    # calls resize_drag() and pointerup calls end_resize().  The geometry is
    # driven from the real pointer position (Gdk seat), not from the event's
    # client coordinates, so it is immune to the page's CSS zoom.  The top-left
    # stays pinned (bottom-right resize).
    def begin_resize(self) -> bool:
        """Remember the window rect and the pointer's screen position."""
        with self._lock:
            try:
                g = self.window_geometry()
                if g is None:
                    return False
                pt = self._pointer_pos()
                if pt is None:
                    return False
                x, y, w, h = g
                self._rs = (x, y, w, h, pt[0], pt[1])
                return True
            except Exception as exc:
                _dbg('begin_resize failed: %s: %s' % (type(exc).__name__, exc))
                return False

    def resize_drag(self) -> bool:
        """Resize so the bottom-right corner tracks the pointer; top-left fixed."""
        with self._lock:
            rs = self._rs
            if not rs:
                return False
            pt = self._pointer_pos()
            if pt is None:
                return False
            x, y, w0, h0, sx, sy = rs
            w, h = self._clamped_size(w0 + (pt[0] - sx), h0 + (pt[1] - sy))
            return bool(self._on_gtk(lambda: self._resize_keep_topleft(x, y, w, h),
                                     False))

    def _resize_keep_topleft(self, x, y, w, h):
        win = self._gtk_window()
        if win is None:
            return False
        try:
            win.resize(int(w), int(h))
            win.move(int(x), int(y))     # keep the top-left pinned against WM drift
            return True
        except Exception as exc:
            _dbg('resize apply failed: %s: %s' % (type(exc).__name__, exc))
            return False

    def end_resize(self) -> bool:
        with self._lock:
            self._rs = None
        # the user chose this size by hand: that is intent, not a transient
        self._user_size = True
        self.save_window_geometry(force=True)
        return True

    # -- window flags -------------------------------------------------------
    def get_window_flags(self) -> dict:
        return {'always_on_top': bool(self.always_on_top),
                'click_through': bool(self.click_through)}

    def toggle_always_on_top(self) -> bool:
        if self._wayland:
            self._report_wayland_once()
            return bool(self.always_on_top)
        want = not bool(self.always_on_top)
        ok = self._on_gtk(lambda: self._set_keep_above(want), False)
        if ok:
            self.always_on_top = want
        return bool(self.always_on_top)

    def _set_keep_above(self, want):
        win = self._gtk_window()
        if win is None:
            return False
        win.set_keep_above(bool(want))
        return True

    def toggle_click_through(self) -> bool:
        if self._wayland:
            self._report_wayland_once()
            return False
        if not self.tray_ok:
            # Safety rule, not a preference: click-through makes the window
            # unclickable, so it must be recoverable from a tray icon.  With no
            # tray there is no way back, so refuse rather than trap the user.
            msg = ('CRT-MEDIA refused to enable click-through: no tray icon '
                   'backend is available, so it could not be turned off again. '
                   'Install %s (sudo apt install %s) and restart the widget.'
                   % (APT_APPINDICATOR, APT_APPINDICATOR))
            _dbg(msg)
            _log('click-through refused: no tray backend')
            _notify(msg)
            return False
        want = not bool(self.click_through)
        ok = self._on_gtk(lambda: self._set_input_shape(want), False)
        if ok:
            self.click_through = want
            _log('click-through %s' % ('on' if want else 'off'))
        return bool(self.click_through)

    def _set_input_shape(self, ignore_mouse):
        """SHAPE-extension click-through on X11.

        ``input_shape_combine_region`` with an empty region makes the window
        ignore the pointer entirely; a region covering the whole window
        restores it.  The toplevel must be realized (get_window() not None).
        """
        win = self._gtk_window()
        if win is None:
            return False
        gdk_window = win.get_window()
        if gdk_window is None:
            _dbg('click-through: toplevel is not realized yet')
            return False
        import cairo
        if ignore_mouse:
            region = cairo.Region()
        else:
            w, h = win.get_size()
            region = cairo.Region(cairo.RectangleInt(0, 0, int(w), int(h)))
        gdk_window.input_shape_combine_region(region, 0, 0)
        return True

    def _report_wayland_once(self):
        if self._wayland_reported:
            return
        self._wayland_reported = True
        text = ('CRT-MEDIA is running in a Wayland session. Always-on-top and '
                'click-through are X11-only, so both are unavailable, and the '
                'window cannot be repositioned by the widget either. Log out '
                'and choose an X11 session for the full behaviour.')
        _log('wayland notice: ' + text)
        try:
            subprocess.Popen(['notify-send', '-a', WINDOW_TITLE,
                              WINDOW_TITLE, text],
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # -- geometry persistence ----------------------------------------------
    def _size_is_deliberate(self, w: int, h: int, user_size: bool) -> bool:
        """Is (w, h) a size the widget should re-settle on?

        Yes when a resize gesture committed it, or when it is clearly not a
        settle transient (a size within SIZE_INTENT_TOL of the design size in
        both axes is noise).  Without this one transient becomes the permanent
        launch target and the corruption is self-perpetuating.
        """
        if user_size:
            return True
        return (abs(w - VIEW_W) > SIZE_INTENT_TOL
                or abs(h - VIEW_H) > SIZE_INTENT_TOL)

    def _load_geometry(self):
        """Remembered {x, y, w, h, user_size} under ~/.local/state, or None."""
        try:
            with open(GEOMETRY_FILE, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            x, y = int(data['x']), int(data['y'])
            if not (-10000 <= x <= 20000 and -10000 <= y <= 20000):
                return None
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
            os.makedirs(STATE_DIR, exist_ok=True)
            # Each write gets its own temp file: the 1 Hz geometry watcher and
            # reset_position() run on different threads, and a shared temp name
            # let one writer's partial file be moved into place by the other.
            self._pos_tmp_seq += 1
            tmp = '%s.%d.tmp' % (GEOMETRY_FILE, self._pos_tmp_seq)
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'x': int(x), 'y': int(y),
                           'w': int(w), 'h': int(h),
                           'user_size': bool(self._user_size)}, fh)
            os.replace(tmp, GEOMETRY_FILE)
        except Exception as exc:
            _dbg('geometry save failed: %s: %s' % (type(exc).__name__, exc))

    def save_window_geometry(self, force: bool = False) -> None:
        # Never let a half-settled startup geometry become the remembered one.
        if not force and not self._geometry_ready.is_set():
            _dbg('geometry not saved: startup geometry is not verified yet')
            return
        g = self.window_geometry()
        if g:
            self.save_geometry(*g)

    def default_position(self):
        """Bottom-right of the primary work area, using the live window size."""
        w, h = VIEW_W + 16, VIEW_H + 40
        g = self.window_geometry()
        if g:
            w, h = g[2], g[3]
        l, t, r, b = self._workarea()
        return max(l, r - w - MARGIN), max(t, b - h - MARGIN)

    def reset_position(self) -> None:
        x, y = self.default_position()
        self._on_gtk(lambda: self._move_to(x, y), False)
        self.save_window_geometry(force=True)

    def _move_to(self, x, y):
        win = self._gtk_window()
        if win is None:
            return False
        win.move(int(x), int(y))
        return True

    def _position_watcher(self) -> None:
        """Persist the geometry ~1 Hz once it has settled.

        The 'same value twice in a row' rule is the debounce: a read taken
        while the window is still being dragged or resized is in flight, not a
        position the user chose.  A resize in flight is skipped entirely -
        end_resize() commits the final size itself.
        """
        last = None      # geometry seen on the previous tick
        saved = None     # geometry already written
        while not self._quit.wait(1.0):
            try:
                if not self._geometry_ready.is_set() or self._rs is not None:
                    last = None
                    continue
                geo = self.window_geometry()
                if geo is None:
                    continue
                if geo != last:
                    last = geo
                    continue
                if geo == saved:
                    continue
                if self.window_geometry() != geo:
                    last = None      # moved again between the two reads
                    continue
                saved = geo
                self.save_geometry(*geo)
            except Exception as exc:
                _dbg('geometry watcher: %s: %s' % (type(exc).__name__, exc))

    # -- viewport settle ----------------------------------------------------
    def _read_viewport(self):
        """(innerWidth, innerHeight, devicePixelRatio) or None."""
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

    def _stable_viewport(self, timeout, changed_from=None):
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
        """Drive the CSS viewport to want_w x want_h, or give up.

        Same discipline as the Windows twin: only act on a value read the same
        way twice, wait for a resize to actually land, and verify the final
        viewport instead of trusting the last resize.  On GTK the window is
        frameless with no CSD, so innerWidth already tracks the window size in
        logical pixels and the correction is applied verbatim.
        """
        want = (int(want_w), int(want_h))
        vp = self._stable_viewport(SETTLE_READ_TIMEOUT)
        if vp is None:
            return None
        repeats = {}
        for step in range(SETTLE_MAX_STEPS):
            if vp[:2] == want:
                _dbg('settle: viewport is the target %dx%d (step %d)' % (want + (step,)))
                return want
            w, h = self._clamped_size(want[0], want[1])
            cur = self.window_geometry()
            if cur is not None and (cur[2], cur[3]) == (w, h):
                _dbg('settle[%d]: resize would be a no-op, stopping' % step)
                break
            if not self._on_gtk(lambda: self._apply_size(w, h), False):
                break
            nxt = self._stable_viewport(SETTLE_READ_TIMEOUT, changed_from=vp)
            if nxt is None:
                break
            key = nxt[:2]
            repeats[key] = repeats.get(key, 0) + 1
            if repeats[key] > 2:
                _dbg('settle: viewport keeps returning to %dx%d - stopping' % key)
                break
            vp = nxt
        final = self._stable_viewport(SETTLE_READ_TIMEOUT)
        _dbg('settle: final viewport %s, target %s' % (final, want))
        return want if (final is not None and final[:2] == want) else None

    def _assert_geometry(self, want_w, want_h):
        """Re-assert (position, size) once after settling, and verify it."""
        if self._want_pos is None:
            self.reset_position()
        else:
            self._on_gtk(lambda: self._move_to(*self._want_pos), False)
        self._on_gtk(lambda: self._apply_size(want_w, want_h), False)
        vp = self._stable_viewport(SETTLE_READ_TIMEOUT)
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
            w, h = VIEW_W, VIEW_H
            l, t, r, b = self._workarea()
            x = max(l, r - (VIEW_W + 16) - MARGIN)
            y = max(t, b - (VIEW_H + 40) - MARGIN)
            self._want_pos = None          # no remembered spot: use the default
            self._user_size = False
        else:
            x, y = saved['x'], saved['y']
            w, h = saved['w'], saved['h']
            self._want_pos = (x, y)
            self._user_size = (bool(saved['user_size'])
                               or (w, h) != (VIEW_W, VIEW_H))
            _dbg('restored geometry %s -> target %dx%d at %d,%d'
                 % (saved, w, h, x, y))
        self._want_view = (w, h)
        if is_wayland():
            self._wayland = True
            self.always_on_top = False     # do not claim what Wayland cannot do

        # resizable=True is deliberate: GTK sets geometry hints that pin the
        # size when a window is non-resizable, and then gtk_window_resize() is
        # ignored - the JS grip could not resize the window at all.  The
        # 260x290 floor is enforced by GTK here and by the bridge clamp.
        self.win = webview.create_window(
            WINDOW_TITLE,
            url=_page_uri(),
            js_api=self.api,
            width=w, height=h,
            x=x, y=y,
            frameless=True, on_top=bool(self.always_on_top), transparent=False,
            background_color=BACKGROUND_COLOR,
            resizable=True, min_size=(VIEW_MIN_W, VIEW_MIN_H),
            easy_drag=False,
        )
        self.win.events.shown += self._on_shown
        self.win.events.closed += self._on_closed

    def _on_shown(self) -> None:
        """Runs on the GTK main thread once the window is really visible."""
        try:
            _load_gtk()
        except MissingBindings as exc:
            _log('binding failure at show: %s' % exc)
            self.force_quit()
            return
        try:
            self.install_press_handler()
        except Exception:
            traceback.print_exc()
        try:
            self.start_tray()
        except Exception:
            _log('tray start failed: %s' % traceback.format_exc())
        if self._wayland:
            self._report_wayland_once()

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

            want_w, want_h = self._want_view
            settled = self._settle_viewport(want_w, want_h)
            if settled is None:
                print('[crt] viewport never settled at %dx%d' % (want_w, want_h),
                      file=sys.stderr)
            else:
                print('[crt] viewport settled at %dx%d' % settled, flush=True)

            try:
                self.win.evaluate_js(
                    'window.__crtApplyZoom && window.__crtApplyZoom()')
            except Exception:
                pass

            placed = self._assert_geometry(want_w, want_h)
            if placed:
                self._geometry_ready.set()
                self.save_window_geometry()
            else:
                print('[crt] geometry not verified at %dx%d; leaving %s untouched'
                      % (want_w, want_h, GEOMETRY_FILE), file=sys.stderr)

            threading.Thread(target=self._position_watcher, daemon=True).start()
            print('[crt] widget ready: %s' % WINDOW_TITLE, flush=True)
        except Exception:
            traceback.print_exc()
            self.force_quit()

    # -- tray ---------------------------------------------------------------
    def start_tray(self) -> None:
        """pystray (appindicator backend) in a daemon thread, with a direct
        AyatanaAppIndicator3 fallback, then no tray at all."""
        try:
            import pystray
            icon = pystray.Icon(
                APP_NAME,
                make_icon_image(),
                WINDOW_TITLE,
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
            self.icon = icon
            # pywebview owns the GTK main loop, so the icon runs in its own
            # daemon thread; pystray's own menu callbacks come back through
            # GLib's idle queue, which _on_gtk() marshals safely.
            threading.Thread(target=icon.run, daemon=True).start()
            self.tray_ok = True
            print('[crt] tray icon started (pystray)', flush=True)
            _log('tray icon started (pystray)')
            return
        except Exception as exc:
            _log('pystray failed: %s: %s' % (type(exc).__name__, exc))
            _dbg('pystray failed: %s: %s' % (type(exc).__name__, exc))
            self._tray_failed = True

        if self._start_indicator_direct():
            return
        _log('no tray backend available; the widget runs without a tray and '
             'click-through will be refused')
        print('[crt] warning: no tray icon backend available', file=sys.stderr)

    def _start_indicator_direct(self) -> bool:
        """Drive AyatanaAppIndicator3 (or AppIndicator3) straight through gi."""
        try:
            G = _load_gtk()
            gi = G['gi']
            Gtk = G['Gtk']
            AppIndicator = None
            for name in (APPINDICATOR_TYPELIB, 'AppIndicator3'):
                try:
                    gi.require_version(name, '0.1')
                    repo = __import__('gi.repository', fromlist=[name])
                    AppIndicator = getattr(repo, name)
                    break
                except Exception:
                    continue
            if AppIndicator is None:
                raise RuntimeError('no appindicator typelib')
            try:
                os.makedirs(STATE_DIR, exist_ok=True)
                make_icon_image(64).save(TRAY_PNG, 'PNG')
                icon_arg = TRAY_PNG
            except Exception:
                icon_arg = APP_NAME
            indicator = AppIndicator.Indicator.new(
                APP_NAME, icon_arg, AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            menu = Gtk.Menu.new()
            for label, cb, check in (
                    ('Always on top', self._tray_always_on_top, 'always_on_top'),
                    ('Click-through', self._tray_click_through, 'click_through')):
                item = Gtk.CheckMenuItem.new_with_label(label)
                item.set_active(bool(getattr(self, check)))
                item.connect('activate', lambda w, fn=cb: fn(None, None))
                menu.append(item)
            reset = Gtk.MenuItem.new_with_label('Reset position')
            reset.connect('activate', lambda w: self._tray_reset(None, None))
            menu.append(reset)
            menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem.new_with_label('Quit')
            quit_item.connect('activate', lambda w: self._tray_quit(None, None))
            menu.append(quit_item)
            menu.show_all()
            indicator.set_menu(menu)
            indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator = indicator
            self.tray_ok = True
            print('[crt] tray icon started (AyatanaAppIndicator3 direct)',
                  flush=True)
            _log('tray icon started (AyatanaAppIndicator3 direct)')
            return True
        except Exception as exc:
            _log('direct appindicator failed: %s: %s' % (type(exc).__name__, exc))
            _dbg('direct appindicator failed: %s: %s' % (type(exc).__name__, exc))
            return False

    def _tray_always_on_top(self, icon, item) -> None:
        self.toggle_always_on_top()

    def _tray_click_through(self, icon, item) -> None:
        self.toggle_click_through()

    def _tray_reset(self, icon, item) -> None:
        self.reset_position()

    def _tray_quit(self, icon, item) -> None:
        self.quit()

    def stop_tray(self) -> None:
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        self.icon = None
        try:
            if self.indicator is not None:
                self.indicator.set_status(0)   # PASSIVE
        except Exception:
            pass
        self.indicator = None

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
        self.save_window_geometry()
        self.stop_tray()
        self.stop_media()
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass

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
    from media_linux import MediaController
    return MediaController()


def _page_uri() -> str:
    from pathlib import Path
    return Path(PAGE).as_uri()


# ---------------------------------------------------------------------------
# js_api - exactly the surface web/app.js calls (plus the window-chrome
# methods the Windows twin's Api exposes, which the tray uses internally)
# ---------------------------------------------------------------------------
class Api:
    """The bridge handed to pywebview.  Media methods pass through unchanged
    to the one shared MediaController; the rest drive the GTK window."""

    def __init__(self, widget: Widget):
        self._w = widget

    # media_linux pass-through
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
def main(argv=None) -> int:
    global DEBUG
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = '--debug' in argv
    DEBUG = bool(DEBUG or debug)

    preflight = run_preflight(report=('--preflight' in argv),
                              dialog=('--preflight' not in argv))
    if preflight != 0:
        return preflight
    if '--preflight' in argv:
        print('[crt] preflight: every required component is present', flush=True)
        print('[crt] media layer: %s' % _media_probe_line(), flush=True)
        return EXIT_OK

    lock = acquire_single_instance()
    if lock is None:
        msg = 'CRT-MEDIA is already running (look for its tray icon).'
        print('[crt] ' + msg, file=sys.stderr)
        if not debug:
            _notify(msg)
        return EXIT_RUNNING

    widget = Widget()
    try:
        import webview
        widget.setup_window()
        webview.start(widget.job, debug=debug, gui='gtk')
    except MissingBindings as exc:
        print(str(exc), file=sys.stderr, flush=True)
        _log('binding failure: %s' % exc)
        _notify(str(exc))
        widget.cleanup()
        return EXIT_MISSING_PREREQ
    except Exception:
        traceback.print_exc()
        widget.cleanup()
        return EXIT_CRASH

    widget.cleanup()
    print('[crt] exited cleanly', flush=True)
    return EXIT_OK


def _entry() -> int:
    """Top-level guard: a .desktop launch has no console, so a crash must be
    written to the log and put on screen with a distinct exit code."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        tb = traceback.format_exc()
        _log('CRASH:\n' + tb)
        tail = '\n'.join(tb.strip().splitlines()[-6:])
        _notify('CRT-MEDIA crashed and has stopped.\n\n%s\n\nFull log:\n%s'
                % (tail, LOG_FILE))
        return EXIT_CRASH


if __name__ == '__main__':
    sys.exit(_entry())
