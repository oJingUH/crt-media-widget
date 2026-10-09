# RETRO-CONTROLLER on Linux (Linux Mint)

This is the Linux twin of the Windows widget in the root of this repository.
Same window, same CRT panel, same web UI (`web/` is shared byte for byte).
What differs is everything underneath it:

| Windows | Linux |
|---|---|
| pywebview on WebView2 / Edge | pywebview on GTK3 + WebKitGTK 4.1 |
| Windows media sessions (SMTC / WinRT) | MPRIS over the D-Bus session bus |
| Core Audio via pycaw | PipeWire via `wpctl` (PulseAudio `pactl` as a fallback) |
| Win32 for drag, resize, always-on-top, click-through | GTK / GDK, plus the X11 SHAPE extension for click-through |
| pystray with the Win32 backend | pystray with the appindicator backend |

Read this file top to bottom once, then do the two commands.

---

## Do these two things

### 1. Tell us about your machine

```
./linux/selfcheck.sh
```

Run it from the project folder. It is **read-only**: it installs nothing,
starts nothing, changes nothing, and it works before setup has ever been run.
It ends with a verdict, and the whole output is plain text meant to be pasted
into a chat message.

It answers the questions you probably cannot answer yourself, in particular
**which session type you are on** (`X11` or `Wayland`), because that decides
whether always-on-top and click-through work at all. It also reports the
bindings, the MPRIS players visible on the bus right now, and which volume
backend you have.

Send that output before doing anything else if you are not sure.

### 2. Install it

```
./linux/setup.sh
```

It checks your distribution and the apt packages the widget imports, then
creates the virtual environment and installs the two pip dependencies. On a
stock Linux Mint 22 everything except `python3-venv` is already installed, so
that one package is the **only** thing that needs `sudo`, and the script prints
the exact command (and offers to run it) rather than doing anything quietly.

At the end it installs the menu entry, the icon and a login autostart entry,
and prints the command to start the widget.

---

## Running it afterwards

Three ways, all equivalent:

| How | Command |
|---|---|
| Applications menu | search for **RETRO-CONTROLLER**, click it |
| From a terminal | `./linux/run.sh` |
| With a log on screen | `./linux/run.sh --debug` |

`./linux/run.sh --preflight` prints the component report and exits, which is
what to use when the widget refuses to start.

Setup also adds an autostart entry, so the widget comes up by itself when you
log in. To stop that:

```
rm -f ~/.config/autostart/retro-controller.desktop
```

Its state lives in `~/.local/state/retro-controller/`:

```
geometry.json   the window position and size it remembers
widget.log      the crash/notice log (written by every run)
tray.png        the generated tray icon
widget.lock     the single-instance lock
```

---

## How it differs from the Windows build, honestly

**Media sessions.** On Linux the widget reads MPRIS on the D-Bus session bus.
The good news is that browsers publish MPRIS properly here, which is the
opposite of Windows: **Firefox 81+** and **Chromium/Chrome** both expose the
playing tab as a media player, so a YouTube tab shows up in the widget.
Electron applications (VS Code, Slack, Discord, Spotify's own client on some
setups) do **not** publish MPRIS, so those will not appear.

**System volume.** There is no pycaw on Linux. The volume slider and the mute
button drive PipeWire through `wpctl`, with `pactl` and `python3-pulsectl` as
fallbacks. The widget tells you which one it found in `--preflight`.

**Always-on-top and click-through need an X11 session.** They are implemented
with GTK's `set_keep_above` and the X11 SHAPE extension respectively, and
neither has an equivalent that a Wayland compositor will honour. Linux Mint's
default Cinnamon session is **X11**, so in practice this is usually fine. If
you are on Wayland the widget does not pretend: it says so once, in the log and
in a desktop notification, and carries on without them. On Wayland it also
cannot move its own window.

**Click-through is refused outright if no tray icon can be created.** That is a
safety rule, not a preference: click-through makes the window ignore the mouse,
and the tray icon is the only way to turn it back off. No tray means no way
back, so the widget declines instead of leaving you with an unusable window.

**The window is opaque.** The rounded case is painted by the page itself, on an
opaque `#06120A` window. Per-pixel transparency through a web view on GTK is
unreliable, and the UI already paints its own case, so it is not attempted.

**No portable build.** The Windows route ships a self-contained bundle. On
Linux you install from source with `setup.sh`; that is the supported route.

---

## What has not been tested

Being blunt about it: **this port has never been executed on a real Linux Mint
machine.** There is no GTK window, no X11 SHAPE call, no tray icon, no MPRIS
player and no apt install in its history. It was written and syntax-checked on
Windows against the pywebview, pystray and GTK sources, and the bridge contract
against the web UI was verified mechanically, but none of the runtime behaviour
here has been observed.

That is exactly why `selfcheck.sh` comes first. It is designed so that the first
run produces a factual report instead of a mystery.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Nothing happens when I click the menu entry | it was launched outside the desktop session, or it crashed with no console | run `./linux/run.sh --debug` in a terminal; read `~/.local/state/retro-controller/widget.log` |
| The window is blank, white or empty | WebKitGTK DMA-BUF rendering on some GPU/driver combinations | `WEBKIT_DISABLE_DMABUF_RENDERER=1 ./linux/run.sh` |
| No tray icon appears | the panel has no status-applet host, or the appindicator typelib is missing | right-click the panel, **Applets**, add **XApp Status Applet**; and `sudo apt install gir1.2-ayatanaappindicator3-0.1` |
| The widget shows nothing to control | nothing is publishing MPRIS | play something in Firefox, Chromium or a player that publishes MPRIS; Electron apps will not appear |
| The status line says `LINK:DEMO` | the bridge never attached | start it with `./linux/run.sh`, not by opening `web/index.html` in a browser |
| Import errors, or a traceback mentioning `gi` | the bindings are missing, or the venv was not made with `--system-site-packages` | re-run `./linux/setup.sh --recreate` |
| Always-on-top does not stick, or the window will not move | you are on a Wayland session | log out and pick an X11 session on the login screen |
| The volume slider does nothing | no `wpctl` and no `pactl` on the machine | `sudo apt install pipewire-bin` (PipeWire) or `pulseaudio-utils` |
| Turning click-through on is refused with a message | no tray backend, so it could not be turned off again | install `gir1.2-ayatanaappindicator3-0.1`, restart the widget, then try again |
| "RETRO-CONTROLLER is already running" | a second instance was launched | use the tray icon of the first one, or quit it first |

---

## Files

| Path | Role |
|---|---|
| `linux/app_linux.py` | The Linux window shell: pywebview on GTK3/WebKitGTK, the `js_api` bridge, GDK drag / resize / always-on-top, X11 SHAPE click-through, the tray icon, geometry memory, single-instance lock, preflight and the no-console crash handler. |
| `linux/setup.sh` | One-time install: distribution and apt package checks, the venv, the pip requirements, the import verification, the menu entry, the icon and the autostart entry. |
| `linux/run.sh` | The launcher behind the menu entry, the autostart entry and `--debug` / `--preflight`. |
| `linux/selfcheck.sh` | The read-only report to run first. Needs no venv. |
| `linux/requirements-linux.txt` | The two pip pins (`pywebview`, `dbus-fast`). Everything else comes from apt. |
| `linux/assets/retro-controller.desktop.in` | The menu entry template; `setup.sh` substitutes `@EXEC@`. |
| `linux/assets/retro-controller.svg` | The icon, installed into the user's hicolor theme. |
| `linux/media_linux.py` | The media layer (MPRIS + PipeWire volume) on its own worker thread. |

The shared, unchanged material lives at the repository root: `app.py` (the
Windows twin), `media.py`, `web/` (the UI, identical on both platforms) and
`requirements.txt` (the Windows pins).
