# CRT-MEDIA // desktop media widget

A small always-on-top, frameless desktop widget that shows what is playing on
this machine — from Spotify, a browser tab, Media Player, anything that
publishes a Windows media session (SMTC) — on a green phosphor CRT, with album
art, a segmented progress bar, transport controls and system volume.

```
+------------------------------------------+
|  CRT-MEDIA // MEDIA CONTROL         v1.0 |
|  SRC:SPOTIFY  VOL:62%  LINK:OK  CLK:07:41|
|  [ album art ]   TITLE / ARTIST / ALBUM  |
|  [==============>----------]  3:32 / 4:57|
|         <<    >||    >>     VOL [====]   |
+------------------------------------------+
```

* **360x400 CSS px**, rounded bezel, scanlines, phosphor bloom, no window frame.
* **Always on top** by default; a tray icon holds the toggles.
* **Click-through** mode so it never blocks what is behind it — and it is
  always recoverable from the tray.
* **Remembers where you dragged it** (a JSON file under
  `%LOCALAPPDATA%\crt-media-widget\`).
* **Resizable** — drag the bracket in the bottom-right corner; the whole panel
  scales with the window (260x290 up to 900x1000) and the size is remembered too.
* **Close button** — the X in the header shuts it down exactly like **Quit** in
  the tray menu.
* **Volume visualizer** — a deliberately fake LED bar meter between the
  transport row and the session chips. Its amplitude is the system volume
  level (drag the volume slider and it follows immediately), not analysed
  audio, and it stays honest: muted or volume 0 collapses it to the baseline,
  a paused track drops it to a dim idle jitter, and no session leaves a flat
  baseline.

Music playback itself is not implemented here. The widget *reads* and *drives*
the media session your apps already publish; it does not play audio.

---

## How to run

From the project folder (`C:\Users\Rubixcube\repos\crt-media-widget`):

| Command | What it does |
|---|---|
| `run.cmd` | Normal launch. Uses the bundled venv's `pythonw.exe`, so **no console window** appears. |
| `run.cmd --debug` | Troubleshooting launch. Uses `python.exe` and **keeps a console open** with the widget's log. |
| `run.vbs` | Silent launch with a hidden window — the form to use from the startup folder. |

You can also run it directly:

```
.venv\Scripts\pythonw.exe app.py
```

Only one widget can run at a time. If you launch a second copy it says so and
exits instead of stacking another window — use the tray icon of the first one.

---

## Start it with Windows

1. Press `Win+R`, type `shell:startup`, press Enter. The Startup folder opens.
2. Right-click **`run.vbs`** in the project folder → **Send to** → **Desktop
   (create shortcut)** (or right-click → **Create shortcut**, then move the
   shortcut into the Startup folder).
3. Leave the shortcut in the Startup folder. Next sign-in the widget appears
   silently in the bottom-right corner.

`run.vbs` is used rather than `run.cmd` because it starts `pythonw.exe` with a
hidden window and returns immediately, so nothing flashes on screen at logon.

To stop it starting with Windows, delete that shortcut again.

---

## The tray icon

A small pixel-art CRT sits in the notification area (you may need to expand the
hidden-icons chevron the first time). Right-click it for:

| Item | Effect |
|---|---|
| **Always on top** *(checkable)* | Pins the widget above other windows. Ticked state always matches reality. |
| **Click-through** *(checkable)* | Makes the widget ignore the mouse so clicks pass to whatever is behind it. **This is the only way to turn it back off**, because while it is on the window cannot be clicked. |
| **Reset position** | Snaps the widget back to the default bottom-right corner and remembers that. |
| **Quit** | Stops the media worker, removes the tray icon and closes the window. |

Click-through is deliberately safe: the tray item stays reachable while the
widget is transparent to the mouse, and the window is kept fully opaque so it
can never become an invisible-but-running trap.

---

## Keyboard shortcuts (when the widget has focus)

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `Right arrow` | Next track |
| `Left arrow` | Previous track |
| `Up arrow` | Volume +5% |
| `Down arrow` | Volume −5% |
| `M` | Mute / unmute system volume |

Clicking anywhere else in the widget does not steal the keys from the focused
app; the shortcuts fire only while the widget window itself has focus.

**Dragging:** grab the bezel (anywhere that is not a button, the progress bar or
the volume bar) and drag. Dragging is done through the Win32 window move, so the
buttons keep working — pywebview's built-in `easy_drag` was turned off for
exactly that reason.

---

## Restyling

Everything visual lives in two places; no Python changes are needed.

* **Palette** — the fixed colour set is defined once as CSS custom properties at
  the top of `web/style.css` (`--bg`, `--panel`, `--dim`, `--base`, `--bright`,
  `--glow`, `--accent1`, `--accent2`, `--warn`, `--scan`). Change a value there
  and the whole widget follows.
* **Font** — `web/fonts/VT323-Regular.ttf`, declared as the `CRT` family in
  `web/style.css`, with `Consolas, monospace` as the fallback. Drop in another
  TTF and update the `@font-face` block to swap the look.

The window's *own* background (`#06120A`) is set in `app.py` as
`BACKGROUND_COLOR`. Keep it matching `--bg`: the page paints its own opaque
case so the rounded corners and the gutter never pick up a light halo from the
unpainted host window.

Layout, animation and the status readout live in `web/index.html` and
`web/app.js`.

---

## What you will see when there is nothing to show

The widget is honest about missing data rather than inventing it:

* **No artwork** for the current track (or artwork not yet extracted) → the art
  block shows a labelled pixel placeholder, not a blank rectangle.
* **No published timeline** — some Chromium sessions report a nonsense
  duration, which the media layer clamps to "no timeline" → the progress bar
  renders a labelled placeholder instead of a fake, moving progress bar.

A track with no artwork or no real timeline is normal, not an error.

---

## Troubleshooting

**The status line says `LINK:DEMO`.**
That means the bridge did not attach: the page is running on its built-in demo
data instead of talking to `app.py`. The window is fine to look at but is not
connected to anything. Causes and fixes:

* You opened `web/index.html` in a normal browser, or ran the screenshot
  harness (`tools/shot.py`), which deliberately runs in demo mode.
* The widget was launched in a way that skipped the Python side — start it with
  `run.cmd`, not by opening the HTML file.
* The bridge thread died. Quit from the tray and relaunch with
  `run.cmd --debug` and read the console.

`LINK:OK` means `get_state()` answered; `LINK:ERR` means the bridge exists but
the media layer returned a failure — try again, or relaunch.

**"No session" / the widget shows nothing to control.**
`has_session: false` simply means nothing is playing and Windows has no media
session to report. Open Spotify, a YouTube tab or Media Player and press play;
the widget will pick it up within about a second. It cannot invent a session,
and pausing or closing the source app makes the session disappear again.

**A second launch does nothing but a message appears.**
An instance is already running. Find its tray icon (expand the hidden-icons
chevron). Quit it there, then relaunch.

**The widget is on screen but will not respond to clicks.**
Click-through is on. Right-click the tray icon and untick **Click-through**.

**The widget is off-screen / behind the taskbar.**
Tray icon → **Reset position**.

**`--debug` shows a traceback about `winrt` or `pycaw`.**
The media layer needs the `winrt-*` and `pycaw` packages installed in the
project venv. Install the project's `requirements` into
`.venv\Scripts\python.exe`; do not add packages to the system Python.

---

## Files

| Path | Role |
|---|---|
| `app.py` | The window shell: pywebview window, `js_api` bridge, Win32 drag / click-through / always-on-top, tray icon, position memory, single-instance guard. |
| `media.py`, `media_selftest.py` | The media layer (WinRT SMTC + pycaw system volume) on a dedicated worker thread. |
| `web/index.html`, `web/style.css`, `web/app.js` | The CRT UI; polls `get_state()` every 600 ms and renders demo mode when no bridge is present. |
| `web/fonts/` | The CRT font. |
| `tools/shot.py` | Screenshot harness that renders the page at 360x400 for visual review. |
| `run.cmd`, `run.vbs` | Launchers (visible-debug / silent). |
| `dev/e2e_probe.py` | End-to-end probe: viewport, live bridge, transport round-trip, click-through recovery, clean exit. |

Configuration written at runtime:

```
%LOCALAPPDATA%\crt-media-widget\position.json   # last window position and size
```
