#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${BB_CODE_VENV:-"$HOME/.bb-code/venv"}"
BIN_DIR="${BB_CODE_BIN_DIR:-"$HOME/.local/bin"}"
PYTHON_BIN="${BB_CODE_PYTHON:-}"

usage() {
  cat <<'USAGE'
Install bb-code so `bb-code` and `build` work from any directory.

Usage:
  ops/install.sh [--venv PATH] [--bin-dir PATH] [--python PATH]

Environment overrides:
  BB_CODE_VENV      Target virtual environment. Default: ~/.bb-code/venv
  BB_CODE_BIN_DIR   Directory for command links. Default: ~/.local/bin
  BB_CODE_PYTHON    Python 3.11+ executable to use.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)
      VENV_DIR="${2:?Missing value for --venv}"
      shift 2
      ;;
    --bin-dir)
      BIN_DIR="${2:?Missing value for --bin-dir}"
      shift 2
      ;;
    --python)
      PYTHON_BIN="${2:?Missing value for --python}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

find_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    printf '%s\n' "$PYTHON_BIN"
    return
  fi

  local candidate
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  echo "Could not find Python 3.11+. Install Python 3.11+ or pass --python /path/to/python." >&2
  exit 1
}

PYTHON_BIN="$(find_python)"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 11):
    version = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(f"Python 3.11+ is required; found {version}. Pass --python /path/to/python3.11")
PY

if [[ ! -f "$PROJECT_ROOT/pyproject.toml" ]]; then
  echo "Installer could not find pyproject.toml at $PROJECT_ROOT" >&2
  exit 1
fi

mkdir -p "$VENV_DIR" "$BIN_DIR"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -e "$PROJECT_ROOT"

ln -sfn "$VENV_DIR/bin/bb-code" "$BIN_DIR/bb-code"
ln -sfn "$VENV_DIR/bin/build" "$BIN_DIR/build"

cat <<EOF
bb-code installed.

Commands:
  $BIN_DIR/bb-code
  $BIN_DIR/build

Add this to your shell profile if it is not already on PATH:
  export PATH="$BIN_DIR:\$PATH"

Try it from any repository:
  build .
EOF
