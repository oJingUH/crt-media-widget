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

## Platform requirements

| | |
|---|---|
| **OS** | Windows 10 or Windows 11, **64-bit** only. |
| **Python** | **Not needed for the portable build.** The from-source route needs CPython 3.11 or newer (developed and tested on 3.14; 3.10 cannot work because pythonnet publishes no wheel for it). Every dependency is a prebuilt wheel, so no C compiler is required. |
| **WebView2 runtime** | Required — it renders the UI. Preinstalled on Windows 11. On Windows 10 you probably have it already through Edge; if the widget's window stays blank or never appears, install the free *Evergreen Standalone Installer* from <https://developer.microsoft.com/microsoft-edge/webview2/>. |
| **.NET Framework 4.8** | Required by pywebview's window host. Preinstalled on fully updated Windows 10 and Windows 11. |

**About volume:** Windows' media session API (SMTC) exposes playback and
metadata but nothing at all about loudness — there is no volume API on that
path. So the volume slider, the mute button and the visualizer read and write
the **Core Audio** endpoint directly, through
[pycaw](https://github.com/AndreMiras/pycaw). That is why volume keeps working
even when no media session is playing.

---

## Run it

### The portable build — no Python required (recommended)

Take `CRT-MEDIA-1.0.0-portable.zip` from the releases page, **extract it
anywhere** (Desktop, `C:\Tools`, a USB stick), and double-click:

| Double-click | What it does |
|---|---|
| **`CRT-MEDIA.vbs`** | Normal, silent start — no console window. |
| **`CRT-MEDIA.bat`** | Same widget, but keeps a console open carrying the log. Use this one when something misbehaves. |

The extracted folder carries its private copy of CPython and every dependency,
so nothing is installed and no Python on the machine is touched. Read
`FIRST-RUN.txt` inside it for the two-click start, the platform notes and where
it keeps its state.

To build that bundle yourself from a checkout:

```
.venv\Scripts\python.exe tools\build_portable.py
```

It produces `dist\CRT-MEDIA-1.0.0-portable\` and the matching `.zip`, and prints
the archive's size and sha256. It downloads the CPython embeddable runtime from
python.org, installs the wheels into it, and refuses to produce a bundle unless
its own interpreter can import every dependency and read a live media session.

### From source

Needs 64-bit Windows and CPython 3.11+ (3.14 tested; 3.10 cannot work because pythonnet publishes no wheel for it).

1. Install Python from <https://www.python.org/downloads/windows/> if you do
   not have it, and tick **Add python.exe to PATH** during the install.
2. **Double-click `setup.cmd`** in the project folder. It needs no admin
   rights. It finds Python, creates `.venv`, installs `requirements.txt` into
   it and verifies that the environment can import `webview`,
   `winrt.windows.media.control`, `pycaw`, `pystray` and `PIL`. It ends with a
   `SUCCESS` line and the next step — or with exactly what failed, and stops
   without leaving a half-installed environment.
3. Start the widget.

| Command | What it does |
|---|---|
| `run.cmd` | Normal launch. Uses the venv's `pythonw.exe`, so **no console window** appears. |
| `run.cmd --debug` | Troubleshooting launch. Uses `python.exe` and **keeps a console open** with the widget's log. |
| `run.vbs` | Silent launch with a hidden window — the form to use from the startup folder. |

You can also run it directly:

```
.venv\Scripts\pythonw.exe app.py
```

If either launcher is run before `setup.cmd` has been, it says so and points at
`setup.cmd` (`run.vbs` raises a dialog box) instead of failing with an obscure
error.

Only one widget can run at a time. If you launch a second copy it says so and
exits instead of stacking another window — use the tray icon of the first one.

---

## Start it with Windows

1. Press `Win+R`, type `shell:startup`, press Enter. The Startup folder opens.
2. Right-click the silent launcher — **`run.vbs`** in the project folder, or
   **`CRT-MEDIA.vbs`** inside the extracted portable folder — → **Send to** →
   **Desktop (create shortcut)** (or right-click → **Create shortcut**, then
   move the shortcut into the Startup folder).
3. Leave the shortcut in the Startup folder. Next sign-in the widget appears
   silently in the bottom-right corner.

The `.vbs` launcher is used rather than `.cmd` because it starts `pythonw.exe`
with a hidden window and returns immediately, so nothing flashes on screen at
logon.

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

**The widget never appears, or its window is blank.**
The WebView2 runtime is missing. Install the free *Evergreen Standalone
Installer* from
<https://developer.microsoft.com/microsoft-edge/webview2/>, then start the
widget again. If you are using the portable build, run `CRT-MEDIA.bat` instead
of `CRT-MEDIA.vbs` first — it keeps a console with the error text on screen.

**`CRT-MEDIA.vbs` / `run.vbs` does nothing at all.**
It raises a dialog box rather than failing silently, so look for a message
window: on the from-source route it means `.venv` does not exist yet and you
need to double-click `setup.cmd` first; on the portable route it means the
folder was not extracted completely — extract the whole zip again, keeping the
folder structure.

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
The media layer's Windows packages are missing from the environment that is
running the widget. On the from-source route, re-run `setup.cmd` — it installs
the project's `requirements.txt` into `.venv\Scripts\python.exe` and then
verifies every import; do not add packages to the system Python. On the
portable route, the folder is incomplete — extract the zip again. To check an
environment by hand:

```
.venv\Scripts\python.exe -c "import webview, winrt.windows.media.control, pycaw, pystray, PIL; print('ok')"
```

---

## Files

| Path | Role |
|---|---|
| `app.py` | The window shell: pywebview window, `js_api` bridge, Win32 drag / click-through / always-on-top, tray icon, position memory, single-instance guard. |
| `media.py`, `media_selftest.py` | The media layer (WinRT SMTC + pycaw system volume) on a dedicated worker thread, plus the headless proof it works. |
| `web/index.html`, `web/style.css`, `web/app.js` | The CRT UI; polls `get_state()` every 600 ms and renders demo mode when no bridge is present. |
| `web/fonts/` | The CRT font. |
| `requirements.txt` | Exact pins for every runtime dependency, all available as Windows x64 wheels. |
| `setup.cmd` | One-time, no-admin, double-clickable from-source install: finds Python, builds `.venv`, installs the requirements and verifies the imports. |
| `run.cmd`, `run.vbs` | Launchers (visible-debug / silent); both point at `setup.cmd` when `.venv` is missing. |
| `tools/build_portable.py` | Builds the self-contained portable bundle (embeddable CPython + wheels + launchers) into `dist/`. |
| `tools/PORTABLE-FIRST-RUN.txt` | The first-run note shipped inside the portable bundle. |
| `tools/shot.py` | Screenshot harness that renders the page at 360x400 for visual review. |
| `dev/e2e_probe.py` | End-to-end probe: viewport, live bridge, transport round-trip, click-through recovery, clean exit. |

Build output (never committed — `dist/` is in `.gitignore`):

```
dist\CRT-MEDIA-1.0.0-portable\       # the runnable bundle
dist\CRT-MEDIA-1.0.0-portable.zip    # the distributable
```

Configuration written at runtime:

```
%LOCALAPPDATA%\crt-media-widget\position.json   # last window position and size
```
