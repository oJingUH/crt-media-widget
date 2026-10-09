#!/usr/bin/env python3
"""RETRO-CONTROLLER end-to-end probe (shell agent).

Drives the LIVE widget from outside the process and proves:

  1. the live CSS viewport is exactly 360x400 (CDP, not a guess);
  2. the live bridge responds (get_state through window.pywebview.api);
  3. one transport command travels UI button -> bridge -> media layer and
     actually flips the media status, then flips back;
  4. click-through toggles on and is recoverable (off again);
  5. a real PIL.ImageGrab screenshot of the widget window, with the
     primary-monitor-relative bbox rule from tools/shot.py, plus mean
     brightness, bright-content bbox and the four corner pixel values
     (corner pixels near --bg #06120A prove there is no light halo).

The widget must be running with --debug so WebView2 exposes its DevTools
protocol port (app.py enables it in debug mode).  The probe speaks CDP over a
hand-rolled RFC6455 WebSocket client, because the project venv has no
websocket package and the venv must not be touched.

Usage:
    "<proj>/.venv/Scripts/python.exe" dev/e2e_probe.py --port 9222 --out dev/live-widget.png

Exit code 0 = every check passed; non-zero = a named check failed.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import struct
import sys
import time
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOW_TITLE = 'RETRO-CONTROLLER'

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> bool:
    print('[%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  ' + detail) if detail else ''))
    if not ok:
        FAILURES.append(name)
    return ok


# ---------------------------------------------------------------------------
# minimal CDP client (RFC6455)
# ---------------------------------------------------------------------------
class CDP:
    def __init__(self, ws_url: str):
        rest = ws_url[5:]
        hostport, path = rest.split('/', 1)
        host, port = hostport.rsplit(':', 1)
        self.sock = socket.create_connection((host, int(port)), timeout=25)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            'GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n'
            'Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n'
            'Sec-WebSocket-Version: 13\r\n\r\n' % (path, hostport, key)
        )
        self.sock.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError('websocket handshake closed by peer')
            buf += chunk
        head, _, self.buf = buf.partition(b'\r\n\r\n')
        if b' 101 ' not in head.split(b'\r\n')[0]:
            raise RuntimeError('websocket handshake failed: %r' % head[:200])
        self._id = 0

    def _recv(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise RuntimeError('websocket closed')
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode: int, data: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack('>H', n)
        else:
            header.append(0x80 | 127)
            header += struct.pack('>Q', n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_message(self) -> dict:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._recv(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            masked, ln = b1 & 0x80, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack('>H', self._recv(2))[0]
            elif ln == 127:
                ln = struct.unpack('>Q', self._recv(8))[0]
            mask = self._recv(4) if masked else None
            payload = self._recv(ln)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                       # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise RuntimeError('websocket closed by peer')
            parts.append(payload)
            if fin:
                break
        return json.loads(b''.join(parts).decode('utf-8'))

    def call(self, method: str, params: dict | None = None):
        self._id += 1
        mid = self._id
        self._send_frame(0x1, json.dumps({'id': mid, 'method': method,
                                          'params': params or {}}).encode())
        while True:
            msg = self._recv_message()
            if msg.get('id') == mid:
                if 'error' in msg:
                    raise RuntimeError('cdp %s error: %s' % (method, msg['error']))
                return msg.get('result')

    def evaluate(self, expr: str, await_promise: bool = True):
        r = self.call('Runtime.evaluate', {
            'expression': expr, 'returnByValue': True, 'awaitPromise': await_promise})
        if r.get('exceptionDetails'):
            raise RuntimeError('js exception for %r: %s'
                               % (expr[:80], json.dumps(r['exceptionDetails'])[:400]))
        return r.get('result', {}).get('value')


def find_page(port: int):
    for _ in range(20):
        try:
            data = json.load(urllib.request.urlopen(
                'http://127.0.0.1:%d/json' % port, timeout=5))
        except Exception:
            time.sleep(0.5)
            continue
        for t in data:
            if t.get('webSocketDebuggerUrl') and t.get('url', '').endswith('index.html'):
                return t
    raise RuntimeError('no index.html CDP target on 127.0.0.1:%d/json' % port)


# ---------------------------------------------------------------------------
# screen capture (primary-monitor relative, per tools/shot.py)
# ---------------------------------------------------------------------------
def init_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def primary_monitor_rect():
    u = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT),
                    ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]

    hmon = u.MonitorFromPoint(wt.POINT(10, 10), 1)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def find_window_rect(title: str):
    u = ctypes.windll.user32
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == title:
            r = wt.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            hits.append((r.left, r.top, r.right, r.bottom))
        return True

    u.EnumWindows(cb, 0)
    return hits[0] if hits else None


def image_stats(img):
    w, h = img.size
    px = img.load()
    total = lit = bright = 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            s = r + g + b
            total += s
            if s > 24:
                lit += 1
            if s > 120:
                bright += 1
                minx, maxx = min(minx, x), max(maxx, x)
                miny, maxy = min(miny, y), max(maxy, y)
    return {
        'size': (w, h),
        'mean_brightness': round(total / (w * h * 3.0), 3),
        'lit_fraction': round(lit / float(w * h), 4),
        'bright_fraction': round(bright / float(w * h), 4),
        'bright_bbox': None if maxx < 0 else (minx, miny, maxx, maxy),
    }


def corner_probe(img, k: int = 3):
    """Opaque case check: sample the outermost ring that the page paints.

    The --bg case fills the window edge, so the true corners and the 1px ring
    inside them must be near-black green, never a light halo (~rgb(239,240,240)).
    """
    w, h = img.size
    px = img.load()
    pts = {
        'top-left(0,0)': (0, 0), 'top-right(w-1,0)': (w - 1, 0),
        'bottom-left(0,h-1)': (0, h - 1), 'bottom-right(w-1,h-1)': (w - 1, h - 1),
        'inset(k,k)': (k, k), 'inset(w-1-k,k)': (w - 1 - k, k),
        'inset(k,h-1-k)': (k, h - 1 - k), 'inset(w-1-k,h-1-k)': (w - 1 - k, h - 1 - k),
        'edge-top-mid(w/2,0)': (w // 2, 0),
        'edge-bottom-mid(w/2,h-1)': (w // 2, h - 1),
        'edge-left-mid(0,h/2)': (0, h // 2),
        'edge-right-mid(w-1,h/2)': (w - 1, h // 2),
    }
    return {name: px[x, y] for name, (x, y) in pts.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='RETRO-CONTROLLER end-to-end probe')
    ap.add_argument('--port', type=int, default=9222)
    ap.add_argument('--out', default=os.path.join(PROJECT, 'dev', 'live-widget.png'))
    ap.add_argument('--wait', type=float, default=1.5, help='settle seconds before capture')
    ap.add_argument('--keep-art', action='store_true', help='do not flip play/pause back')
    args = ap.parse_args(argv)

    init_dpi()
    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print('=== RETRO-CONTROLLER end-to-end probe ===')
    print('project   %s' % PROJECT)
    print('cdp port  %d' % args.port)

    page = find_page(args.port)
    print('target    %s' % page.get('url'))
    print('ws        %s' % page['webSocketDebuggerUrl'])
    cdp = CDP(page['webSocketDebuggerUrl'])

    # --- 2. viewport ------------------------------------------------------
    vp = cdp.evaluate('JSON.stringify([window.innerWidth, window.innerHeight])')
    iw, ih = json.loads(vp)
    print('[viewport] innerWidth=%d innerHeight=%d' % (iw, ih))
    check('viewport is exactly 360x400', (iw, ih) == (360, 400),
          'got %dx%d' % (iw, ih))

    # --- bridge attached? -------------------------------------------------
    ready = cdp.evaluate('!!(window.pywebview && window.pywebview.api)')
    print('[bridge] pywebview.api present: %s' % ready)
    check('bridge attached', bool(ready))

    status = cdp.evaluate("document.getElementById('statusText').textContent")
    print('[status] %r' % status)
    check('status readout is not LINK:DEMO', 'LINK:DEMO' not in (status or ''),
          'status=%r' % status)

    api_methods = cdp.evaluate(
        "JSON.stringify(['get_state','get_art','play_pause','next_track',"
        "'previous_track','seek_fraction','set_volume','toggle_mute',"
        "'select_session','start_drag','get_window_flags',"
        "'toggle_click_through','toggle_always_on_top','quit_app']"
        ".filter(function(n){return typeof window.pywebview.api[n] !== 'function';}))")
    missing = json.loads(api_methods)
    check('every bridge method the UI calls exists', not missing,
          'missing=%s' % missing)

    # --- state through the bridge ----------------------------------------
    st = cdp.evaluate('window.pywebview.api.get_state()')
    print('[state] ok=%s has_session=%s app=%s status=%s title=%r pos=%s dur=%s'
          % (st.get('ok'), st.get('has_session'), st.get('app_name'),
             st.get('status'), st.get('title'), st.get('position'), st.get('duration')))
    check('get_state() answers on the live bridge', isinstance(st, dict) and st.get('ok') is True,
          'ok=%s error=%s' % (st.get('ok'), st.get('error')))

    # --- 4. transport round trip -----------------------------------------
    before = st.get('status')
    if st.get('has_session') and before in ('playing', 'paused'):
        clicked = cdp.evaluate("(document.getElementById('btnPlay').click(), 'clicked')")
        print('[transport] clicked #btnPlay -> %s' % clicked)
        time.sleep(1.6)
        after = cdp.evaluate('window.pywebview.api.get_state()')
        print('[transport] status before=%s after=%s' % (before, after.get('status')))
        check('play/pause through the UI flipped the media status',
              after.get('status') != before,
              'before=%s after=%s' % (before, after.get('status')))
        if not args.keep_art:
            cdp.evaluate("(document.getElementById('btnPlay').click(), 'clicked')")
            time.sleep(1.6)
            restored = cdp.evaluate('window.pywebview.api.get_state()')
            print('[transport] status restored=%s' % restored.get('status'))
            check('status restored to the original value',
                  restored.get('status') == before,
                  'want=%s got=%s' % (before, restored.get('status')))
    else:
        print('[transport] SKIPPED: no playing/paused session is live '
              '(has_session=%s status=%s) - cannot prove a real transport flip'
              % (st.get('has_session'), before))

    # --- 5. click-through recovery ---------------------------------------
    flags0 = cdp.evaluate('window.pywebview.api.get_window_flags()')
    print('[flags ] before        %s' % json.dumps(flags0, sort_keys=True))
    on = cdp.evaluate('window.pywebview.api.toggle_click_through()')
    flags1 = cdp.evaluate('window.pywebview.api.get_window_flags()')
    print('[flags ] click-through %s  -> %s' % (on, json.dumps(flags1, sort_keys=True)))
    check('click-through can be turned ON', on is True and flags1.get('click_through') is True,
          'toggle=%s flags=%s' % (on, flags1))
    off = cdp.evaluate('window.pywebview.api.toggle_click_through()')
    flags2 = cdp.evaluate('window.pywebview.api.get_window_flags()')
    print('[flags ] click-through %s  -> %s' % (off, json.dumps(flags2, sort_keys=True)))
    check('click-through can be turned back OFF (recoverable)',
          off is False and flags2.get('click_through') is False,
          'toggle=%s flags=%s' % (off, flags2))
    check('get_window_flags has exactly the two contract keys',
          set(flags2.keys()) == {'always_on_top', 'click_through'},
          'keys=%s' % sorted(flags2.keys()))

    # --- 3. screenshot ----------------------------------------------------
    time.sleep(max(0.0, args.wait))
    rect = find_window_rect(WINDOW_TITLE)
    if rect is None:
        check('widget window found on screen', False, 'no visible "%s" window' % WINDOW_TITLE)
        return 1
    print('[window] rect(physical)=%s  size=%dx%d  primary=%s'
          % (rect, rect[2] - rect[0], rect[3] - rect[1], primary_monitor_rect()))

    from PIL import ImageGrab
    px1, py1, px2, py2 = primary_monitor_rect()
    x1, y1, x2, y2 = rect
    if not (px1 <= x1 and py1 <= y1 and x2 <= px2 and y2 <= py2):
        check('window fully inside the primary monitor', False, 'rect=%s primary=%s'
              % (rect, (px1, py1, px2, py2)))
        return 1
    bbox = (x1 - px1, y1 - py1, x2 - px1, y2 - py1)   # primary-relative, no virtual shift
    img = ImageGrab.grab(bbox=bbox).convert('RGB')
    stats = image_stats(img)
    corners = corner_probe(img)
    img.save(out_path)
    img.close()

    print('[shot] %s' % out_path)
    print('[shot] size=%dx%d  mean_brightness=%.3f  lit=%.2f%%  bright=%.2f%%  bright_bbox=%s'
          % (stats['size'][0], stats['size'][1], stats['mean_brightness'],
             stats['lit_fraction'] * 100.0, stats['bright_fraction'] * 100.0,
             stats['bright_bbox']))
    print('[corners] (want near --bg #06120A = (6,18,10), never ~(239,240,240))')
    for name, val in corners.items():
        print('    %-26s %s' % (name, val))

    check('capture is not blank', stats['mean_brightness'] > 2.0,
          'mean=%.3f' % stats['mean_brightness'])
    halo = {k: v for k, v in corners.items()
            if min(v) > 150 or sum(v) > 400}
    check('no light halo at the window edges/corners', not halo, 'offenders=%s' % halo)

    print('=== %d check(s) failed ===' % len(FAILURES) if FAILURES
          else '=== ALL CHECKS PASSED ===')
    for f in FAILURES:
        print('  FAILED: %s' % f)
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
