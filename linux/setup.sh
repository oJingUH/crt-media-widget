#!/usr/bin/env bash
# ===========================================================================
#  RETRO-CONTROLLER // setup (Linux) - one-time, from-source install
#
#  Run it from the project folder:
#
#      ./linux/setup.sh
#
#  What it does:
#    1. checks the distribution (Linux Mint, Ubuntu or Debian);
#    2. checks every apt package the widget imports.  On Linux Mint 22 the
#       GTK/WebKit/dbus/PIL bindings are already installed and python3-venv is
#       the only one missing - that is the single sudo point in this script.
#       With -y and an interactive terminal it offers to run that one apt
#       command; without a terminal it prints the literal command and stops;
#    3. creates ../.venv with --system-site-packages, using uv when uv is
#       present and python3 -m venv otherwise.  --system-site-packages is
#       essential: it is what exposes the apt gi bindings to the venv (a plain
#       venv cannot import them, and pip-installing PyGObject needs a compiler
#       and dev headers that are not on the ISO);
#    4. pip-installs linux/requirements-linux.txt into that venv;
#    5. verifies the venv can import gi, Gtk 3.0, WebKit2 4.1, webview, PIL and
#       dbus, naming the first failure and exiting non-zero;
#    6. installs the .desktop entry and icon into ~/.local, adds an autostart
#       entry, and refreshes the desktop database.
#
#  It installs nothing system-wide beyond step 2's apt command, which it asks
#  about first.
#
#  Options:
#      --recreate     delete and rebuild ../.venv from scratch
#      --no-autostart do not add the widget to the login autostart list
# ===========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python3"
REQ="$HERE/requirements-linux.txt"
DESKTOP_IN="$HERE/assets/retro-controller.desktop.in"
ICON_SVG="$HERE/assets/retro-controller.svg"

APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
AUTOSTART_DIR="$HOME/.config/autostart"

RECREATE=0
AUTOSTART=1
for arg in "$@"; do
  case "$arg" in
    --recreate)     RECREATE=1 ;;
    --no-autostart) AUTOSTART=0 ;;
    -h|--help)
      sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- the apt packages this widget imports, and which apt package provides them
APT_PACKAGES=(
  python3-gi                            # gi
  python3-gi-cairo                      # Gtk/Gdk drawing (pulls python3-cairo)
  python3-cairo                         # import cairo - X11 click-through
  gir1.2-gtk-3.0                        # Gtk 3.0 typelib
  gir1.2-webkit2-4.1                    # WebKit2 4.1 typelib (the renderer)
  python3-dbus                          # dbus
  python3-pil                           # PIL (tray icon, artwork)
  python3-xlib                          # X11 pointer fallback for the grip
  gir1.2-ayatanaappindicator3-0.1       # the tray icon
  zenity                                # error dialogs when there is no console
  libnotify-bin                         # notify-send
)
VENV_PACKAGE='python3-venv'

# ---------------------------------------------------------------------------
die() {
  printf '\n[FAIL] %s\n' "$1" >&2
  if [[ -n "${2:-}" ]]; then
    printf '\n       Fix:\n         %s\n' "$2" >&2
  fi
  printf '\nSetup stopped. Nothing was left in a half-installed state.\n' >&2
  exit 1
}

say() { printf '%s\n' "$1"; }
head2() { printf '\n[%s] %s\n' "$1" "$2"; }

printf '===========================================================================\n'
printf ' RETRO-CONTROLLER // setup (Linux)\n'
printf '===========================================================================\n'
printf ' project   %s\n' "$ROOT"
printf ' venv      %s\n' "$VENV"
printf '\n Everything happens inside this folder and the new .venv it creates;\n'
printf ' the only system-wide step is the apt package check below.\n'

[[ -f "$REQ" ]] || die "linux/requirements-linux.txt is missing from
       $REQ
       Run setup.sh from inside the RETRO-CONTROLLER project folder." \
      "cd $ROOT && ./linux/setup.sh"
[[ -f "$DESKTOP_IN" ]] || die "the desktop entry template is missing: $DESKTOP_IN"
[[ -f "$ICON_SVG" ]] || die "the icon is missing: $ICON_SVG"

# ---------------------------------------------------------------------------
head2 1/6 "checking the distribution"

[[ -r /etc/os-release ]] || die "/etc/os-release does not exist, so this does
       not look like a Linux system this script understands."
# shellcheck disable=SC1091
. /etc/os-release
DISTRO_ID="${ID:-unknown}"
DISTRO_LIKE="${ID_LIKE:-}"
DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
DISTRO_VER="${VERSION_ID:-?}"
say "       $DISTRO_NAME (id=$DISTRO_ID version=$DISTRO_VER)"

case " $DISTRO_ID $DISTRO_LIKE " in
  *' linuxmint '*|*' ubuntu '*|*' debian '*) : ;;
  *) die "unsupported distribution: $DISTRO_NAME (id='$DISTRO_ID').
       This script targets Linux Mint, Ubuntu and Debian, because it checks
       and installs apt packages with dpkg-query/apt-get. Nothing was changed." \
      "Install the packages by hand; linux/LINUX.md lists them." ;;
esac

# ---------------------------------------------------------------------------
head2 2/6 "checking the apt packages the widget imports"

MISSING=()
for pkg in "${APT_PACKAGES[@]}"; do
  if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
    say "       ok      $pkg"
  else
    say "       MISSING $pkg"
    MISSING+=("$pkg")
  fi
done

VENV_PKG_OK=1
if ! dpkg-query -W -f='${Status}' "$VENV_PACKAGE" 2>/dev/null | grep -q 'install ok installed'; then
  say "       MISSING $VENV_PACKAGE  (needed to create the virtual environment)"
  VENV_PKG_OK=0
  MISSING+=("$VENV_PACKAGE")
fi

APT_CMD="sudo apt-get install -y ${MISSING[*]:-}"

if [[ ${#MISSING[@]} -gt 0 ]]; then
  say ""
  say "       ${#MISSING[@]} package(s) are missing. The exact command is:"
  say ""
  say "           $APT_CMD"
  say ""
  if [[ -t 0 && -t 1 ]]; then
    read -r -p "       Run it now? [y/N] " reply || reply=n
    case "$reply" in
      y|Y|yes|YES)
        say "       running: $APT_CMD"
        # shellcheck disable=SC2086  # deliberate word splitting of the command
        $APT_CMD || die "the apt command failed - see its output above." "$APT_CMD"
        say "       apt finished"
        ;;
      *)
        die "the missing packages were not installed, so setup cannot continue." \
            "$APT_CMD" ;;
    esac
  else
    die "this shell has no interactive terminal, so setup will not run sudo on
       its own. Run the command above yourself, then run setup.sh again." \
        "$APT_CMD"
  fi
else
  say "       every apt package is already installed"
fi

# Re-query after a possible apt install.  The pre-apt VENV_PKG_OK flag goes
# stale the moment python3-venv is installed in the block above; trusting it
# forced a pointless second setup run on stock Mint 22.
if dpkg-query -W -f='${Status}' "$VENV_PACKAGE" 2>/dev/null | grep -q 'install ok installed'; then
  VENV_PKG_OK=1
else
  VENV_PKG_OK=0
fi

if [[ $VENV_PKG_OK -eq 0 ]]; then
  die "$VENV_PACKAGE is still missing; python3 -m venv and uv venv both need it." \
      "$APT_CMD"
fi

# ---------------------------------------------------------------------------
head2 3/6 "creating $VENV"

if [[ $RECREATE -eq 1 && -d "$VENV" ]]; then
  say "       --recreate: removing the existing .venv"
  rm -rf "$VENV"
fi

if [[ -d "$VENV" && ! -x "$PY" ]]; then
  # A venv without bin/python3 is not a Linux venv (a Windows checkout leaves
  # .venv/Scripts/python.exe behind).  Refuse rather than half-use it.
  die "$VENV exists but has no bin/python3, so it is not a Linux virtual
       environment (a Windows .venv looks like this)." \
      "rm -rf '$VENV' && ./linux/setup.sh"
fi

if [[ -x "$PY" ]]; then
  say "       .venv already exists"
else
  if command -v uv >/dev/null 2>&1; then
    say "       creating .venv with uv (--system-site-packages)"
    uv venv --system-site-packages --python /usr/bin/python3 "$VENV" \
      || die "'uv venv' failed - see its output above." \
             "uv venv --system-site-packages --python /usr/bin/python3 '$VENV'"
  else
    say "       uv was not found; creating .venv with python3 -m venv"
    python3 -m venv --system-site-packages "$VENV" \
      || die "'python3 -m venv' failed. The usual cause is that the python3-venv
       package is not installed (or is installed for a different python)." \
             "sudo apt-get install -y python3-venv && ./linux/setup.sh"
  fi
fi

[[ -x "$PY" ]] || die "$PY does not exist after creating the environment." \
  "rm -rf '$VENV' && ./linux/setup.sh"

# Prove --system-site-packages really is in effect before wasting a download.
if ! "$PY" -c 'import sys, site; sys.exit(0 if site.ENABLE_USER_SITE is not None and any("dist-packages" in p for p in sys.path) else 1)'; then
  say "       warning: this venv does not appear to see the system site-packages"
  say "                (the apt gi bindings would not be importable from it)"
fi

# ---------------------------------------------------------------------------
head2 4/6 "installing linux/requirements-linux.txt"

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PY" -r "$REQ" \
    || die "'uv pip install' failed - see its output above." \
           "uv pip install --python '$PY' -r '$REQ'"
else
  "$PY" -m pip install --upgrade pip \
    || die "'python -m pip install --upgrade pip' failed - see the output above." \
           "'$PY' -m pip install --upgrade pip"
  "$PY" -m pip install -r "$REQ" \
    || die "'pip install -r linux/requirements-linux.txt' failed - see its output
       above." \
           "'$PY' -m pip install -r '$REQ'"
fi

# ---------------------------------------------------------------------------
head2 5/6 "verifying the new environment"

if ! "$PY" - <<'PY'
import sys

# (import it, apt package that provides it, what it is for)
CHECKS = [
    ('import gi',                        'python3-gi',                 'the gi bindings'),
    ('gi.require_version("Gtk", "3.0")', 'python3-gi / gir1.2-gtk-3.0', 'GTK 3 bindings'),
    ('from gi.repository import Gtk',    'gir1.2-gtk-3.0',             'the Gtk 3.0 typelib'),
    ('gi.require_version("WebKit2", "4.1")', 'gir1.2-webkit2-4.1',     'the WebKitGTK 4.1 typelib'),
    ('from gi.repository import WebKit2', 'gir1.2-webkit2-4.1',        'the renderer'),
    ('import webview',                   'pywebview (pip)',             'the window + js_api bridge'),
    ('import PIL',                       'python3-pil',                'the tray icon / artwork'),
    ('import dbus',                      'python3-dbus',               'MPRIS over the session bus'),
]

first_failure = None
name_space = {}          # one shared namespace: `import gi` must be visible to
for stmt, package, what in CHECKS:   # the require_version/from checks that follow
    try:
        exec(stmt, name_space)
    except Exception as exc:
        print('       FAIL %-34s %s' % (stmt, exc))
        if first_failure is None:
            first_failure = (stmt, package, what, exc)
    else:
        print('       ok   %s' % stmt)

if first_failure is not None:
    stmt, package, what, exc = first_failure
    print()
    print('       the first failure was: %s' % stmt)
    print('       that is %s (%s)' % (what, package))
    print('       error: %s: %s' % (type(exc).__name__, exc))
    sys.exit(1)
print()
print('       imports OK: gi, Gtk 3.0, WebKit2 4.1, webview, PIL, dbus')
PY
then
  die "the new environment was built, but an import above failed.
       Nothing else was changed. Try:
           ./linux/setup.sh --recreate
       to build .venv again from scratch and read the pip/uv output above." \
      "./linux/setup.sh --recreate"
fi

# ---------------------------------------------------------------------------
head2 6/6 "installing the menu entry, the icon and the autostart entry"

mkdir -p "$APPS_DIR" "$ICON_DIR"
if [[ $AUTOSTART -eq 1 ]]; then
  mkdir -p "$AUTOSTART_DIR"
fi

# The Exec line is the venv's python plus the app, both absolute and quoted so
# a path with spaces still works.
EXEC_LINE="\"$PY\" \"$HERE/app_linux.py\""
DESKTOP_OUT="$APPS_DIR/retro-controller.desktop"

sed "s|@EXEC@|${EXEC_LINE}|" "$DESKTOP_IN" > "$DESKTOP_OUT" \
  || die "could not write $DESKTOP_OUT"

if grep -q '@EXEC@' "$DESKTOP_OUT"; then
  die "@EXEC@ was not substituted in $DESKTOP_OUT - the template is broken." \
      "sed 's|@EXEC@|$EXEC_LINE|' '$DESKTOP_IN' > '$DESKTOP_OUT'"
fi

chmod 644 "$DESKTOP_OUT"
install -m 644 "$ICON_SVG" "$ICON_DIR/retro-controller.svg"
say "       menu entry  $DESKTOP_OUT"
say "       icon        $ICON_DIR/retro-controller.svg"

if [[ $AUTOSTART -eq 1 ]]; then
  cp -f "$DESKTOP_OUT" "$AUTOSTART_DIR/retro-controller.desktop"
  say "       autostart   $AUTOSTART_DIR/retro-controller.desktop"
else
  say "       autostart   skipped (--no-autostart)"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 \
    && say "       desktop database refreshed" \
    || say "       note: update-desktop-database reported a problem (harmless)"
else
  say "       note: update-desktop-database is not installed; the entry still works"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
printf '\n===========================================================================\n'
printf ' SUCCESS - RETRO-CONTROLLER is set up and ready to run.\n'
printf '===========================================================================\n\n'
printf ' Start the widget now with:\n\n'
printf '     ./linux/run.sh\n\n'
printf ' or pick "RETRO-CONTROLLER" from the applications menu. From this point on it\n'
printf ' also starts automatically when you log in%s.\n' \
  "$( [[ $AUTOSTART -eq 1 ]] && printf '' || printf ' (disabled by --no-autostart)' )"
printf '\n If something misbehaves, this keeps a log on screen:\n\n'
printf '     ./linux/run.sh --debug\n\n'
printf ' Its state lives in ~/.local/state/retro-controller/ (widget.log and\n'
printf ' geometry.json). Remove the autostart entry to stop it starting at login:\n\n'
printf '     rm -f ~/.config/autostart/retro-controller.desktop\n\n'
exit 0
