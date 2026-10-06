#!/usr/bin/env python3
"""CRT-MEDIA screenshot harness (ui agent).

Loads web/index.html in a real pywebview window sized to a 360x400 CSS-pixel
viewport at a fixed screen position, captures that exact screen region with
PIL.ImageGrab, and prints machine-readable facts about every shot (absolute
path, pixel size, mean brightness, lit fraction, ink bounding box) so a blank
or mis-aligned capture is obvious from text alone.

Usage (exact form required by CONTRACT.md):

    "<proj>/.venv/Scripts/python.exe" tools/shot.py \
        --demo 1 --out dev/shot-settled.png --wait 3.0 \
        --boot-shot dev/shot-boot.png

Exit codes
    0  success, every printed metric clean
    2  bad arguments
    3  window / page load failure
    4  capture failure, or a blank / black capture
    5  selftest metrics missing or dirty
    6  watchdog: the run did not finish in time
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import traceback

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(PROJECT, 'web', 'index.html')
WINDOW_TITLE = 'CRT-MEDIA-SHOT'

VIEW_W, VIEW_H = 360, 400
DEFAULT_POS = (2160, 980)          # near the bottom-right of the primary screen
BLANK_MEAN = 2.0                   # mean brightness below this = black capture

FAIL = {}


# --------------------------------------------------------------------------
# dpi / screen helpers
# --------------------------------------------------------------------------
def init_dpi() -> str:
    """Make the process DPI aware so window rects and ImageGrab agree."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v2
        return 'per-monitor-v2'
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return 'system'
    except Exception:
        return 'none'


def screen_origin() -> tuple[int, int]:
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77)   # SM_X/YVIRTUALSCREEN


def find_window_handle(pid: int, title: str):
    """HWND of our visible top-level window with this exact title."""
    u = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        wpid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value == title:
            hits.append(hwnd)
        return True

    u.EnumWindows(cb, 0)
    return hits[0] if hits else None


def raise_topmost(hwnd) -> None:
    """Force a window to the top of the topmost band (above the backdrop)."""
    if not hwnd:
        return
    u = ctypes.windll.user32
    HWND_TOPMOST = wt.HWND(-1)
    SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)


def find_window_rect(pid: int, title: str):
    """Physical rect of our visible top-level window with this exact title."""
    u = ctypes.windll.user32
    hits: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lparam):
        wpid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid or not u.IsWindowVisible(hwnd):
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


def primary_monitor_rect():
    """Physical rect of the primary monitor."""
    u = ctypes.windll.user32

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT),
                    ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]

    hmon = u.MonitorFromPoint(wt.POINT(10, 10), 1)   # MONITOR_DEFAULTTOPRIMARY
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def grab_rect(rect, origin=None):
    """Grab the exact screen rect into a PIL image.

    Empirically on this host ImageGrab's bbox is primary-monitor relative and
    must NOT be shifted by SM_X/YVIRTUALSCREEN (verified against a known
    capture: offsetting by the virtual origin captured a region 228 px low).
    """
    from PIL import ImageGrab
    x1, y1, x2, y2 = rect
    px1, py1, px2, py2 = primary_monitor_rect()
    if not (px1 <= x1 and py1 <= y1 and x2 <= px2 and y2 <= py2):
        raise RuntimeError(
            'window rect %s is not fully inside the primary monitor %s; the '
            'capture mapping for a secondary monitor is not verified here'
            % (rect, (px1, py1, px2, py2)))
    bbox = (x1 - px1, y1 - py1, x2 - px1, y2 - py1)
    img = ImageGrab.grab(bbox=bbox)
    if img.size != (x2 - x1, y2 - y1):
        raise RuntimeError('grab size %s does not match the window rect %s'
                           % (img.size, (x2 - x1, y2 - y1)))
    return img.convert('RGB')


def image_stats(img) -> dict:
    """Mean brightness + two coverage measures.

    lit    : any pixel above near-black (sum > 24)  - catches a totally blank grab
    bright : pixel sum > 120                        - ignores the --bg panel fill and
             catches the 1px --dim bezel outline, so bright_bbox/insets prove the
             captured region lines up with the window (expected inset ~5px).
    """
    w, h = img.size
    px = img.load()
    total = 0
    lit = 0
    bright = 0
    n = w * h
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
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    bbox = None if maxx < 0 else (minx, miny, maxx, maxy)
    return {
        'size': (w, h),
        'mean_brightness': round(total / (n * 3.0), 3),
        'lit_fraction': round(lit / float(n), 4),
        'bright_fraction': round(bright / float(n), 4),
        'bright_bbox': bbox,
        'insets': None if bbox is None else (bbox[0], bbox[1], w - 1 - bbox[2], h - 1 - bbox[3]),
    }


# --------------------------------------------------------------------------
def parse_flag(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ('0', 'false', 'no', 'off', '')


def build_url(demo: bool, selftest: bool, state: str | None, extra: str | None = None) -> str:
    """Build the page URL.

    Flags go in the fragment ('#demo=1'), not the query: WebView2 percent-encodes
    the '?' of a file:// URL and the load dies with ERR_FILE_NOT_FOUND (verified
    on this host).  app.js reads both channels.
    """
    from pathlib import Path
    url = Path(PAGE).as_uri()
    bits = []
    if demo:
        bits.append('demo=1')
    if selftest:
        bits.append('selftest=1')
    if state:
        bits.append('state=' + state)
    if extra:
        bits.append(extra.strip('&#?'))
    return url + ('#' + '&'.join(bits) if bits else '')


def report_shot(name, path, stats):
    print('[shot] %-9s %s' % (name, os.path.abspath(path)))
    print('        size=%dx%d  mean_brightness=%.3f  lit=%.2f%%  bright=%.2f%%  '
          'bright_bbox=%s  insets=%s'
          % (stats['size'][0], stats['size'][1], stats['mean_brightness'],
             stats['lit_fraction'] * 100.0, stats['bright_fraction'] * 100.0,
             stats['bright_bbox'], stats['insets']))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='CRT-MEDIA screenshot harness')
    ap.add_argument('--demo', default='1', help='1 => append demo=1 (default 1)')
    ap.add_argument('--out', required=True, help='settled-shot PNG path')
    ap.add_argument('--wait', type=float, default=3.0, help='seconds after load before the settled shot')
    ap.add_argument('--boot-shot', default=None, help='extra PNG captured early, during the boot sequence')
    ap.add_argument('--boot-wait', type=float, default=0.7, help='seconds after load for --boot-shot (default 0.7)')
    ap.add_argument('--state', default=None, choices=['paused', 'playing'], help='force the rendered state')
    ap.add_argument('--selftest', default='1', help='1 => append selftest=1 and print window.__SELFTEST__')
    ap.add_argument('--width', type=int, default=VIEW_W)
    ap.add_argument('--height', type=int, default=VIEW_H)
    ap.add_argument('--x', type=int, default=DEFAULT_POS[0])
    ap.add_argument('--y', type=int, default=DEFAULT_POS[1])
    ap.add_argument('--timeout', type=float, default=45.0, help='watchdog seconds')
    ap.add_argument('--allow-dirty', action='store_true', help='do not fail on dirty metrics')
    ap.add_argument('--flags', default=None,
                    help='extra page flags appended to the URL fragment, e.g. "art=0" or "na=1"')
    ap.add_argument('--backdrop', default='1',
                    help='1 => also put a plain opaque plate behind the widget, so the '
                         'shots never depend on the desktop underneath (default 1)')
    args = ap.parse_args(argv)

    demo = parse_flag(args.demo)
    selftest = parse_flag(args.selftest)
    backdrop = parse_flag(args.backdrop)
    out_path = os.path.abspath(args.out)
    boot_path = os.path.abspath(args.boot_shot) if args.boot_shot else None
    url = build_url(demo, selftest, args.state, args.flags)

    for p in [out_path] + ([boot_path] if boot_path else []):
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                print('[fail] cannot create output directory %s: %s' % (d, e))
                return 2

    dpi_mode = init_dpi()
    origin = screen_origin()

    import webview

    print('[crt-shot] page      %s' % PAGE)
    print('[crt-shot] url       %s' % url)
    print('[crt-shot] dpi=%s  virtual_origin=%s  requested_pos=(%d,%d)  viewport=%dx%d'
          % (dpi_mode, origin, args.x, args.y, args.width, args.height))

    def watchdog():
        time.sleep(args.timeout)
        msg = '[fail] watchdog: run exceeded %.1fs' % args.timeout
        print(msg)
        sys.stdout.flush()
        os._exit(6)

    threading.Thread(target=watchdog, daemon=True).start()

    BACK_PAD = 16
    back = None
    if backdrop:
        back = webview.create_window(
            WINDOW_TITLE + '-BACKDROP',
            html='<!doctype html><html><head><meta charset="utf-8"></head>'
                 '<body style="margin:0;width:100%;height:100%;background:#0B0F0C"></body></html>',
            width=args.width + 2 * BACK_PAD, height=args.height + 2 * BACK_PAD,
            x=args.x - BACK_PAD, y=args.y - BACK_PAD,
            frameless=True, on_top=True, transparent=False,
            resizable=False, easy_drag=False, shadow=False,
            background_color='#0B0F0C',
        )

    win = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=args.width, height=args.height,
        x=args.x, y=args.y,
        frameless=True, on_top=True, transparent=True,
        resizable=False, easy_drag=False, shadow=False,
        background_color='#06120A',
    )

    def job():
        shots = []
        selftest_data = None
        try:
            if not win.events.loaded.wait(20):
                raise RuntimeError('page did not finish loading within 20s')
            t0 = time.monotonic()

            # --- force the CSS viewport to exactly width x height -------------
            fixed = None
            for _ in range(20):
                try:
                    vp = json.loads(win.evaluate_js(
                        'JSON.stringify([window.innerWidth, window.innerHeight])'))
                except Exception as e:
                    raise RuntimeError('could not read the viewport: %s' % e)
                vp = tuple(vp)
                dx, dy = args.width - vp[0], args.height - vp[1]
                if dx == 0 and dy == 0:
                    fixed = vp
                    break
                try:
                    win.resize(win.width + dx, win.height + dy)
                except Exception as e:
                    raise RuntimeError('window resize failed: %s' % e)
                time.sleep(0.05)
            if fixed is None:
                raise RuntimeError('viewport never settled at %dx%d (last %s)'
                                   % (args.width, args.height, vp))
            print('[crt-shot] viewport settled at %dx%d (pywebview pre-adjusts by '
                  'its own frame delta)' % fixed)

            # the backdrop plate is also topmost: keep the widget above it
            raise_topmost(find_window_handle(os.getpid(), WINDOW_TITLE))
            time.sleep(0.2)

            rect = find_window_rect(os.getpid(), WINDOW_TITLE)
            if rect is None:
                raise RuntimeError('could not locate the harness window on screen')
            print('[crt-shot] window_rect(physical)=%s  size=%dx%d  primary=%s'
                  % (rect, rect[2] - rect[0], rect[3] - rect[1], primary_monitor_rect()))

            def shoot(name, path):
                img = grab_rect(rect)
                size = img.size
                stats = image_stats(img)
                img.save(path)
                img.close()
                if size != stats['size']:
                    raise RuntimeError('capture %s changed size while measuring' % path)
                report_shot(name, path, stats)
                shots.append((name, path, stats))
                if stats['mean_brightness'] <= BLANK_MEAN:
                    raise RuntimeError(
                        'capture %s looks blank/black (mean brightness %.3f <= %.1f)'
                        % (path, stats['mean_brightness'], BLANK_MEAN))

            if boot_path:
                wait = args.boot_wait - (time.monotonic() - t0)
                if wait > 0:
                    time.sleep(wait)
                shoot('boot', boot_path)

            wait = args.wait - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
            shoot('settled', out_path)

            if selftest:
                time.sleep(0.25)
                raw = win.evaluate_js(
                    'window.__selftest ? window.__selftest() : '
                    '(window.__SELFTEST__ ? JSON.stringify(window.__SELFTEST__) : null)')
                if not raw:
                    FAIL['code'] = 5
                    print('[fail] selftest requested but the page exposed no __SELFTEST__ object')
                else:
                    selftest_data = json.loads(raw)
                    print('[selftest] ' + '-' * 60)
                    # complete + machine-readable on one line: nothing is truncated,
                    # and the summary fields stay greppable in a log
                    print(json.dumps(selftest_data, separators=(',', ':'), sort_keys=True))
                    print('[selftest] ' + '-' * 60)
        except Exception:
            FAIL['trace'] = traceback.format_exc()
        finally:
            for w in (win, back):
                try:
                    if w is not None:
                        w.destroy()
                except Exception:
                    pass
            FAIL['shots'] = shots
            FAIL['selftest'] = selftest_data

    try:
        webview.start(job, debug=False)
    except Exception:
        FAIL['trace'] = (FAIL.get('trace', '') + '\n' + traceback.format_exc()).strip()

    if FAIL.get('trace'):
        print('[fail] harness error:')
        print(FAIL['trace'])
        return 3

    shots = FAIL.get('shots') or []
    if len(shots) < (2 if boot_path else 1):
        print('[fail] expected %d capture(s), got %d' % (2 if boot_path else 1, len(shots)))
        return 4

    st = FAIL.get('selftest')
    dirty = []
    if selftest:
        if not st:
            dirty.append('selftest metrics missing')
        else:
            if st.get('overflow', {}).get('any'):
                dirty.append('element overflow: %s' % st['overflow'].get('offenders'))
            if st.get('scroll', {}).get('overflowX') or st.get('scroll', {}).get('overflowY'):
                dirty.append('document scrollbars: %s' % st.get('scroll'))
            if not st.get('font', {}).get('loaded'):
                dirty.append('CRT font not loaded: %s' % st.get('font'))
            if not st.get('colors', {}).get('contrastOk'):
                dirty.append('contrast below 4.5:1: %s' % st.get('colors', {}).get('contrastRatio'))
            if not st.get('transport', {}).get('ok'):
                dirty.append('transport hit targets below 44x36: %s'
                             % {k: st.get('transport', {}).get(k) for k in ('minHitW', 'minHitH')})
    for name, _p, s in shots:
        if s['mean_brightness'] <= BLANK_MEAN:
            dirty.append('%s shot mean brightness %.3f' % (name, s['mean_brightness']))
        ins = s['insets']
        if ins is None:
            dirty.append('%s shot has no bright pixels at all (blank plate)' % name)
        elif min(ins) > 12:
            dirty.append('%s shot content starts inset %s px: capture region probably '
                         'mis-aligned with the window' % (name, ins))

    print('[crt-shot] shots=%d  mean_brightness=%s'
          % (len(shots), [s['mean_brightness'] for _n, _p, s in shots]))
    if dirty:
        print('[crt-shot] METRICS: DIRTY')
        for d in dirty:
            print('   - ' + d)
        if not args.allow_dirty:
            return 5
    else:
        print('[crt-shot] METRICS: CLEAN (no overflow, no scrollbars, font loaded, '
              'contrast>=4.5, hit targets>=44x36, capture not blank)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
