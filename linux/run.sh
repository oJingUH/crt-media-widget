#!/usr/bin/env bash
# ===========================================================================
#  RETRO-CONTROLLER // run (Linux)
#
#  Start the widget from a checkout.  Used by the .desktop entry, by the
#  autostart entry, and by hand from a terminal.
#
#  Usage:
#      ./linux/run.sh                normal start
#      ./linux/run.sh --debug        keep a console with the widget's log
#      ./linux/run.sh --preflight    print the component report and exit
#
#  If the virtual environment does not exist yet it says so and points at
#  linux/setup.sh instead of failing with an obscure error.
# ===========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python3"
APP="$HERE/app_linux.py"

if [[ ! -f "$APP" ]]; then
  echo
  echo "  RETRO-CONTROLLER cannot start: app_linux.py is missing from"
  echo "      $HERE"
  echo
  echo "  This does not look like a complete RETRO-CONTROLLER checkout."
  echo
  exit 2
fi

if [[ ! -x "$PY" ]]; then
  echo
  echo "  RETRO-CONTROLLER is not set up yet on this machine."
  echo
  echo "  The project's virtual environment is missing:"
  echo "      $VENV"
  echo
  echo "  Run the setup once, from the project folder:"
  echo
  echo "      ./linux/setup.sh"
  echo
  echo "  When it reports SUCCESS, start the widget again with"
  echo "      ./linux/run.sh            (or ./linux/run.sh --debug)"
  echo "  or pick RETRO-CONTROLLER from the applications menu."
  echo
  exit 1
fi

exec "$PY" "$APP" "$@"
