# CRT media widget — frozen interface contract

This file is the single source of truth for names and shapes. Every builder codes
against **this**, never against another builder's in-flight file. If something here
is wrong or impossible, report it instead of quietly diverging.

## Project root

The project folder is this repository. Run every command from it, and read every
path below as relative to it.

Interpreter (use this exact path for every run and test):

```
.venv/Scripts/python.exe
```

Installed already: `winrt-runtime`, `winrt-Windows.Foundation`,
`winrt-Windows.Foundation.Collections`, `winrt-Windows.Media.Control`,
`winrt-Windows.Storage.Streams`, `pycaw`, `comtypes`, `pywebview`, `pystray`,
`pillow`. Python is 3.14.7. Do not add dependencies without saying so in your report.

## File ownership (hard fence — do not write outside your own list)

| Path | Owner |
|---|---|
| `media.py`, `media_selftest.py` | media agent |
| `web/index.html`, `web/style.css`, `web/app.js` | ui agent |
| `tools/shot.py` | ui agent |
| `app.py`, `run.cmd`, `run.vbs`, `README.md` | shell agent |
| `web/fonts/*` | provisioned by the orchestrator — read only |
| `dev/*` | nobody ships here; use the scratch dir for throwaway work |

Nobody runs state-changing git commands in this tree (no commit, checkout, stash,
reset, push). Read-only git is fine.

## Palette (fixed — both sides use these exact values)

```
--bg        #06120A   background, near-black green
--panel     #0A1B0F   slightly raised areas
--dim       #1E7A34   borders, inactive, track
--base      #33FF66   primary phosphor
--bright    #A6FFB8   headings, hover
--glow      rgba(51,255,102,.55)
--accent1   #FF2E88   magenta
--accent2   #22D3EE   cyan
--warn      #FF3B30
--scan      rgba(0,0,0,.22)
```

Font: `web/fonts/VT323-Regular.ttf`, `@font-face` family name `CRT`, fallback
`Consolas, monospace`. Assume **no CP437 box-drawing glyph coverage** — draw frames
with CSS borders and corner ticks, not with `╔═╗` characters.

## Python side: `media.py`

```python
class MediaController:
    def get_state(self) -> dict
    def get_art(self, art_key: str) -> str | None   # returns a data: URL or None
    def play_pause(self) -> bool
    def next_track(self) -> bool
    def previous_track(self) -> bool
    def seek_fraction(self, fraction: float) -> bool   # 0.0 .. 1.0 of duration
    def set_volume(self, level: float) -> bool         # 0.0 .. 1.0, SYSTEM volume
    def toggle_mute(self) -> bool
    def select_session(self, app_id: str) -> bool      # "" means follow the system's current session
    def shutdown(self) -> None
```

### Required concurrency design

All Windows Runtime and Core Audio work must happen on **one dedicated worker
thread** that owns the WinRT session manager, with public methods marshalling onto
it (queue plus lock) and a hard timeout of about 2 seconds. Rationale: the WinRT
manager is apartment-sensitive, pywebview calls in from its own threads, and the UI
polls this method every 600 ms — a call must never hang the caller. `get_state()`
must return in well under 50 ms in the normal case and must never extract artwork.

### `get_state()` — exact shape, every key always present

```json
{
  "ok": true,
  "has_session": true,
  "app_id": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
  "app_name": "Spotify",
  "title": "Voulez-Vous",
  "artist": "ABBA",
  "album": "Voulez-Vous",
  "status": "playing",
  "position": 212.036,
  "duration": 297.892,
  "can_seek": true,
  "can_next": true,
  "can_previous": true,
  "art_key": "ab12cd34",
  "volume": 0.62,
  "muted": false,
  "sessions": [
    {"app_id": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", "app_name": "Spotify",
     "title": "Voulez-Vous", "artist": "ABBA", "status": "playing"}
  ],
  "t_ms": 4712.4,
  "error": null
}
```

Rules:

- `status` is one of `playing`, `paused`, `stopped`, `closed`, `unknown`.
- `position` and `duration` are float seconds, or `null` when the app publishes no
  usable timeline. Chromium sessions report a sentinel like `-1 day` — clamp that to
  `null`, never pass it through.
- `art_key` is a short stable hash (first 8 hex chars of a hash of the raw artwork
  bytes), `null` when there is no artwork. Artwork bytes are **not** in this payload.
- `can_seek` is true only when the session reports a real duration and enables
  position changes.
- `volume` and `muted` are system endpoint values from pycaw, independent of session.
- `t_ms` is `time.monotonic() * 1000` at the moment of the read. The UI extrapolates
  the progress bar from this against its own clock.
- `sessions` lists every live session, newest first, capped at 8.
- Any failure: `ok: false`, `error` a short human string, other keys present with
  safe fallbacks (`has_session: false`). This method must not raise, ever.

### `get_art(art_key)`

Returns `"data:image/png;base64,..."` for a key previously reported, or `None` if it
is unknown or expired. Downscale to at most 160x160 before encoding. Cache at least
the last 8 artworks, keyed by `art_key`.

## JS side: the bridge

`app.py` passes a `js_api` object whose method names mirror `MediaController`
exactly, so the UI calls:

```js
const st = await window.pywebview.api.get_state();
const art = await window.pywebview.api.get_art(st.art_key);
```

Additional bridge methods provided by `app.py`:

```js
window.pywebview.api.start_drag()                  // call on pointerdown over the bezel
window.pywebview.api.get_window_flags()            // -> {always_on_top: bool, click_through: bool}
window.pywebview.api.toggle_click_through()        // -> bool (new value)
window.pywebview.api.toggle_always_on_top()        // -> bool (new value)
window.pywebview.api.quit_app()
```

### UI requirements against the bridge

- Poll `get_state()` every 600 ms; refresh immediately after any command.
- Every bridge call wrapped in try/catch. **When `window.pywebview` is absent the UI
  must run in demo mode**: `?demo=1` in the URL, or no bridge detected, switches to a
  built-in `DEMO_STATE` (playing, a fake track, a duration that advances with
  `performance.now()`), so the page renders standalone in a browser and in the
  screenshot harness.
- Demo mode must be honest about being demo: the status line shows `LINK:DEMO`.
- Progress bar extrapolates from `position` + `t_ms` while `status === "playing"`.
- Do not call `get_art` when `art_key` is unchanged from the last poll.

## Window

- Default 360x400 CSS px, positioned near the bottom-right of the primary screen,
  frameless, transparent, always-on-top, not resizable by drag.
- Dragging is manual, not pywebview's `easy_drag` (which swallows clicks on buttons):
  pointerdown on the bezel calls `start_drag()`.
- Click-through is toggled from the **tray icon**, never from inside the window
  (once click-through is on, the window cannot receive clicks).
- Tray menu: Always on top, Click-through, Reset position, Quit.

## Aesthetic requirements (finite list — stop when all ten are done and reviewed)

1. Rounded bezel, 14px radius, 1px `--dim` inner border, inset glow, vignette.
2. Scanline overlay: 1px on / 3px off, `--scan`, `pointer-events: none`, above content.
3. Phosphor bloom: layered `text-shadow` using `--glow` on all text.
4. Chromatic fringe: magenta/cyan ±1px on the header only, low opacity.
5. Subtle flicker: opacity 0.97 to 1.0 over about 3.5s, barely perceptible.
6. Boot sequence: about 1.6s of typed lines (`CRT-MEDIA v1.0`, `PHOSPHOR P1 ... OK`,
   `SMTC LINK ... OK`, `AUDIO BUS ... OK`), click-to-skip, must not delay the first poll.
7. Blinking block cursor after the status line.
8. Album art block: pixelated downscale, green duotone treatment, CSS frame with corner ticks.
9. Segmented progress bar with `elapsed / total` in monospace.
10. Status readout line, e.g. `SRC:SPOTIFY  VOL:62%  LINK:OK  CLK:07:41`.

Also: `user-select: none`, no scrollbars, no text cursor, transport hit targets at
least 44x36, hover brightens, active inverts. Space toggles play/pause, arrows skip,
up/down change volume when the window has focus.

## Screenshot harness: `tools/shot.py` (ui agent)

Run it exactly like this:

```
"<proj>/.venv/Scripts/python.exe" tools/shot.py --demo 1 --out dev/shot-settled.png --wait 3.0 --boot-shot dev/shot-boot.png
```

- Loads `web/index.html` in a real pywebview window at the default 360x400 size,
  positioned at a fixed on-screen spot, above a plain backdrop.
- Waits, then grabs that exact screen region with `PIL.ImageGrab` and saves a PNG.
- When `--boot-shot` is given, also captures early during the boot sequence.
- Always closes the window and exits 0 on success, non-zero with a printed reason on failure.
- `--state paused` optionally forces the paused rendering.

The orchestrator will look at these PNGs. The harness must print the absolute path,
the pixel size and the mean pixel brightness of each shot so a blank or black capture
is obvious from text alone.
