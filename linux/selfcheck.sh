#!/usr/bin/env bash
# ===========================================================================
#  CRT-MEDIA // selfcheck (Linux)
#
#  THE FIRST THING TO RUN.  It works before setup.sh has ever been run, needs
#  no virtual environment, and changes nothing on the machine: it only reads.
#
#      ./linux/selfcheck.sh
#
#  Paste the whole output into a chat message.  It is plain text on purpose:
#  no colours, no spinners, labelled sections, one ok/warn/FAIL marker per
#  check, and a verdict with the exact next command at the end.
#
#  It deliberately does NOT use `set -e`: a selfcheck that aborts on the first
#  surprise is useless.  Every check prints something either way.
# ===========================================================================

PY=/usr/bin/python3
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"

if [[ -z "$PY" || ! -x "$PY" ]]; then
  echo "No python3 found (/usr/bin/python3 is missing). This is not a normal"
  echo "Linux Mint install; report that line and stop here."
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

WARN=0
FAIL=0
ok()   { printf '  [ok]   %s\n' "$1"; }
note() { printf '         %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; WARN=$((WARN + 1)); }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=$((FAIL + 1)); }
hr()   { printf '\n---------------------------------------------------------------------------\n'; }
section() { hr; printf ' %s. %s\n' "$1" "$2"; hr; }

printf '===========================================================================\n'
printf ' CRT-MEDIA // selfcheck\n'
printf '===========================================================================\n'
printf ' project    %s\n' "$ROOT"
printf ' python3    %s (%s)\n' "$PY" "$("$PY" --version 2>&1)"
printf ' date       %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')"
printf ' run as     %s\n' "$(id -un 2>/dev/null || echo '?')"
printf '\n Read-only: this script installs nothing, starts nothing and changes\n'
printf ' nothing. It reports facts and ends with a verdict.\n'

# ---------------------------------------------------------------------------
section 1 "system / distribution"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  printf '  distro     %s\n' "${PRETTY_NAME:-unknown}"
  printf '  id         %s\n' "${ID:-unknown}"
  printf '  version    %s (%s)\n' "${VERSION_ID:-?}" "${VERSION_CODENAME:-?}"
  case " ${ID:-} ${ID_LIKE:-} " in
    *' linuxmint '*|*' ubuntu '*|*' debian '*)
      ok "a distribution setup.sh supports (Linux Mint / Ubuntu / Debian)" ;;
    *)
      warn "setup.sh targets Linux Mint / Ubuntu / Debian; this is '${ID:-unknown}'" ;;
  esac
else
  bad "/etc/os-release is not readable"
fi
printf '  arch       %s\n' "$(uname -m)"
printf '  kernel     %s\n' "$(uname -r)"
printf '  bash       %s\n' "${BASH_VERSION:-?}"

# ---------------------------------------------------------------------------
section 2 "desktop session"
printf '  XDG_CURRENT_DESKTOP  %s\n' "${XDG_CURRENT_DESKTOP:-<unset>}"
printf '  XDG_SESSION_TYPE     %s\n' "${XDG_SESSION_TYPE:-<unset>}"
printf '  DISPLAY              %s\n' "${DISPLAY:-<unset>}"
printf '  WAYLAND_DISPLAY      %s\n' "${WAYLAND_DISPLAY:-<unset>}"
SESSION_TYPE="$(printf '%s' "${XDG_SESSION_TYPE:-}" | tr '[:upper:]' '[:lower:]')"
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  bad "no DISPLAY and no WAYLAND_DISPLAY: there is no desktop session here."
  note "If you are running this over SSH, that is why. Run it in a terminal"
  note "on the Mint desktop itself (or use 'ssh -X')."
elif [[ "$SESSION_TYPE" == "wayland" ]]; then
  warn "this is a Wayland session: always-on-top and click-through are"
  note "X11-only, so the widget runs without them, and it cannot move its own"
  note "window either. Log out and choose an 'X11'/'Cinnamon (Software"
  note "Rendering)' style session on the login screen for the full behaviour."
  note "Linux Mint's default Cinnamon session is X11, so this is unusual."
elif [[ "$SESSION_TYPE" == "x11" ]]; then
  ok "X11 session (${XDG_CURRENT_DESKTOP:-?}): every feature is available"
else
  warn "XDG_SESSION_TYPE is '${XDG_SESSION_TYPE:-<unset>}'; cannot tell whether"
  note "the window manager will honour always-on-top. DISPLAY is ${DISPLAY:-<unset>}."
fi

# ---------------------------------------------------------------------------
section 3 "apt packages the widget imports"
APT_PACKAGES=(
  python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
  python3-dbus python3-pil python3-xlib gir1.2-ayatanaappindicator3-0.1
  zenity libnotify-bin
)
APT_MISSING=()
for pkg in "${APT_PACKAGES[@]}"; do
  if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
    ok "$pkg"
  else
    bad "$pkg is NOT installed"
    APT_MISSING+=("$pkg")
  fi
done
if dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed'; then
  ok "python3-venv"
else
  warn "python3-venv is not installed (expected on a stock Mint 22; setup.sh"
  note "offers to install it - it is the one thing that needs sudo)"
fi

# ---------------------------------------------------------------------------
section 4 "D-Bus session bus (MPRIS lives here)"
printf '  DBUS_SESSION_BUS_ADDRESS  %s\n' "${DBUS_SESSION_BUS_ADDRESS:-<unset>}"
if "$PY" -c 'import dbus' >/dev/null 2>&1; then
  ok "the system python can import dbus"
  if "$PY" - <<'PY'
import sys
try:
    import dbus
    bus = dbus.SessionBus()
    names = bus.list_names()
    print('  [ok]   connected to the session bus (%d names on it)' % len(names))
except Exception as exc:
    print('  [FAIL] could not use the session bus: %s: %s' % (type(exc).__name__, exc))
    sys.exit(1)
PY
  then :; else bad "no usable session bus"; fi
else
  bad "python3-dbus is not importable from $PY"
fi

# ---------------------------------------------------------------------------
section 5 "MPRIS players visible right now"
note "This is what the widget reads. Browsers DO publish MPRIS (Firefox 81+,"
note "Chromium); Electron apps and some native players do not."
"$PY" - <<'PY'
try:
    import dbus
    bus = dbus.SessionBus()
    names = sorted(str(n) for n in bus.list_names()
                   if str(n).startswith('org.mpris.MediaPlayer2.'))
except Exception as exc:
    print('  [FAIL] could not enumerate MPRIS players: %s: %s'
          % (type(exc).__name__, exc))
    raise SystemExit(0)

if not names:
    print('  [warn] no MPRIS player is on the bus right now.')
    print('         That is normal when nothing is playing. Start a track in a')
    print('         browser or a music player and run this again.')
    raise SystemExit(0)

for name in names:
    print('  [ok]   %s' % name)
    try:
        obj = bus.get_object(name, '/org/mpris/MediaPlayer2')
        props = dbus.Interface(obj, 'org.freedesktop.DBus.Properties')
        try:
            ident = props.Get('org.mpris.MediaPlayer2', 'Identity')
        except Exception:
            ident = '?'
        try:
            status = props.Get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
        except Exception:
            status = '?'
        try:
            meta = props.Get('org.mpris.MediaPlayer2.Player', 'Metadata')
        except Exception:
            meta = {}
        title = meta.get('xesam:title')
        artist = meta.get('xesam:artist')
        if artist is not None and not isinstance(artist, str):
            try:
                artist = ', '.join(str(a) for a in artist)
            except Exception:
                artist = str(artist)
        length = meta.get('mpris:length')
        print('           Identity        %s' % ident)
        print('           PlaybackStatus  %s' % status)
        print('           title           %s' % (title if title is not None else '<none>'))
        print('           artist          %s' % (artist if artist else '<none>'))
        if length is None:
            print('           mpris:length    absent  -> no timeline; the widget')
            print('                           renders the progress bar as unavailable')
        else:
            try:
                secs = float(length) / 1000000.0
                print('           mpris:length    present (%.3f s) -> the progress bar works'
                      % secs)
            except Exception:
                print('           mpris:length    present (%r)' % (length,))
    except Exception as exc:
        print('           could not read properties: %s: %s'
              % (type(exc).__name__, exc))
PY

# ---------------------------------------------------------------------------
section 6 "system volume backend"
VOL_FOUND=0
if command -v wpctl >/dev/null 2>&1; then
  ok "wpctl (PipeWire) at $(command -v wpctl)"
  VOL_FOUND=1
  OUT="$(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>&1)" && {
    printf '         wpctl get-volume @DEFAULT_AUDIO_SINK@ -> %s\n' "$OUT"
    case "$OUT" in
      *MUTED*) note "currently MUTED" ;;
    esac
  } || note "wpctl could not read the default sink: $OUT"
else
  warn "wpctl is not installed (no PipeWire control)"
fi
if command -v pactl >/dev/null 2>&1; then
  ok "pactl (PulseAudio / pipewire-pulse)"
  VOL_FOUND=1
  V="$(pactl get-sink-volume @DEFAULT_SINK@ 2>&1)" && printf '         pactl get-sink-volume -> %s\n' "$(printf '%s' "$V" | head -n1)"
  M="$(pactl get-sink-mute @DEFAULT_SINK@ 2>&1)" && printf '         pactl get-sink-mute   -> %s\n' "$M"
else
  note "pactl is not installed (that is fine if wpctl is present)"
fi
if "$PY" -c 'import pulsectl' >/dev/null 2>&1; then
  ok "python3-pulsectl is importable"
  VOL_FOUND=1
else
  note "python3-pulsectl is not importable (optional; python3-pulsectl)"
fi
if [[ $VOL_FOUND -eq 0 ]]; then
  bad "no volume backend at all (wpctl, pactl and python3-pulsectl are all absent)"
  note "The volume slider and mute button have nothing to drive."
fi

# ---------------------------------------------------------------------------
section 7 "GTK 3 + WebKitGTK 4.1 bindings (system python)"
"$PY" - <<'PY'
import sys
checks = [
    ('import gi', 'python3-gi'),
    ('gi.require_version("Gtk", "3.0")', 'gir1.2-gtk-3.0'),
    ('from gi.repository import Gtk', 'gir1.2-gtk-3.0'),
    ('gi.require_version("WebKit2", "4.1")', 'gir1.2-webkit2-4.1'),
    ('from gi.repository import WebKit2', 'gir1.2-webkit2-4.1'),
    ('import cairo', 'python3-cairo (via python3-gi-cairo)'),
]
failed = 0
name_space = {}          # one shared namespace: `import gi` must be visible to
for stmt, package in checks:   # the require_version/from checks that follow it
    try:
        exec(stmt, name_space)
    except Exception as exc:
        print('  [FAIL] %s  (%s)' % (stmt, package))
        print('         %s: %s' % (type(exc).__name__, exc))
        failed += 1
    else:
        print('  [ok]   %s' % stmt)
if failed:
    print('')
    print('         Fix: sudo apt install python3-gi python3-gi-cairo '
          'gir1.2-gtk-3.0 gir1.2-webkit2-4.1')
    sys.exit(1)
PY
if [[ $? -ne 0 ]]; then
  bad "the GTK 3 / WebKitGTK 4.1 bindings are not importable (see above)"
else
  ok "GTK 3 and WebKitGTK 4.1 are importable from the system python"
fi

# ---------------------------------------------------------------------------
section 8 "can a real GTK window be created here?"
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  warn "skipped: there is no display in this shell"
else
  GTK_TEST=/usr/bin/python3
  [[ -x "$GTK_TEST" ]] || GTK_TEST="$PY"
  GTK_PROBE='import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk; Gtk.Window(); print("ok")'
  if command -v timeout >/dev/null 2>&1; then
    CREATED="$(timeout 20 "$GTK_TEST" -c "$GTK_PROBE" 2>&1)"
  else
    CREATED="$("$GTK_TEST" -c "$GTK_PROBE" 2>&1)"
  fi
  if printf '%s' "$CREATED" | grep -q '^ok$'; then
    ok "a Gtk.Window could be constructed (the toolkit talks to the display)"
  else
    bad "Gtk.Window() failed: $(printf '%s' "$CREATED" | head -n2)"
    note "If DISPLAY is set but GTK cannot open it, check that you are on the"
    note "local desktop session and not inside a root/SSH shell."
  fi
  # A short-lived off-screen window proves the full round trip through the
  # window manager without flashing anything in the middle of the screen.
  if command -v timeout >/dev/null 2>&1; then
    if timeout 20 "$GTK_TEST" - <<'PY' >/tmp/.crt-selfcheck-gtk.$$ 2>&1
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib
w = Gtk.Window(title='crt-selfcheck')
w.set_skip_taskbar_hint(True)
w.set_default_size(120, 80)
w.move(-4000, -4000)
w.show_all()
GLib.timeout_add(400, Gtk.main_quit)
Gtk.main()
print('window shown and closed')
PY
    then
      ok "a Gtk.Window was shown and closed by the window manager"
      cat /tmp/.crt-selfcheck-gtk.$$
    else
      warn "could not show a Gtk.Window (output below)"
      cat /tmp/.crt-selfcheck-gtk.$$
    fi
    rm -f /tmp/.crt-selfcheck-gtk.$$
  else
    note "no 'timeout' command: skipped the live window test so nothing can hang"
  fi
fi

# ---------------------------------------------------------------------------
section 9 "tray icon backend"
if "$PY" - <<'PY'
import sys
try:
    import gi
    for name in ('AyatanaAppIndicator3', 'AppIndicator3'):
        try:
            gi.require_version(name, '0.1')
            __import__('gi.repository', fromlist=[name])
            print('  [ok]   %s 0.1 is available' % name)
            sys.exit(0)
        except Exception:
            continue
    print('  [warn] neither AyatanaAppIndicator3 nor AppIndicator3 is available')
    print('         Fix: sudo apt install gir1.2-ayatanaappindicator3-0.1')
    print('         Without a tray backend the widget refuses to enable')
    print('         click-through, so the window can never become unclickable.')
    sys.exit(0)
except Exception as exc:
    print('  [warn] could not probe the appindicator typelibs: %s' % exc)
PY
then :; fi

# ---------------------------------------------------------------------------
hr
printf ' VERDICT\n'
hr
if [[ $FAIL -gt 0 ]]; then
  printf '  NOT READY - %d check(s) failed, %d warning(s).\n\n' "$FAIL" "$WARN"
  printf '  What to do next, in this order:\n\n'
  if [[ ${#APT_MISSING[@]} -gt 0 ]]; then
    printf '   1. install the missing packages (this is the only sudo step):\n\n'
    printf '        sudo apt install %s\n\n' "${APT_MISSING[*]}"
    printf '   2. then run the setup:\n\n        ./linux/setup.sh\n\n'
  else
    printf '   1. run the setup:\n\n        ./linux/setup.sh\n\n'
    printf '   2. if it still fails, send this whole output back.\n\n'
  fi
else
  if [[ $WARN -gt 0 ]]; then
    printf '  READY, with %d warning(s).\n\n' "$WARN"
  else
    printf '  READY - every check passed.\n\n'
  fi
  printf '  Next command:\n\n      ./linux/setup.sh\n\n'
  printf '  (run it from the project folder; it needs sudo once, for\n'
  printf '   python3-venv, and asks before using it.)\n\n'
  printf '  Then start the widget with:\n\n      ./linux/run.sh\n\n'
fi
printf '  Copy everything above this line into the chat message.\n'
printf '===========================================================================\n'

exit 0
