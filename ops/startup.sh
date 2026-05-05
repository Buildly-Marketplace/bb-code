#!/usr/bin/env bash
# ops/startup.sh - Manage bb-code local web workspace (start|stop|restart)
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${BB_CODE_VENV:-"$ROOT_DIR/.venv"}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8791}"
PID_FILE="$ROOT_DIR/.bb/bb-code.pid"
LOG_FILE="$ROOT_DIR/.bb/bb-code.log"

ensure_venv() {
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"
}

start_service() {
  mkdir -p "$ROOT_DIR/.bb"
  ensure_venv
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
    echo "bb-code is already running with PID $(cat "$PID_FILE")."
    return
  fi
  echo "Starting bb-code at http://$HOST:$PORT ..."
  nohup "$VENV_DIR/bin/build" --host "$HOST" --port "$PORT" --no-open "$ROOT_DIR" > "$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"
}

stop_service() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" >/dev/null 2>&1; then
    echo "Stopping bb-code PID $(cat "$PID_FILE") ..."
    kill "$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
  else
    echo "No running bb-code process found."
  fi
}

case "${1:-start}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service
    start_service
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 2
    ;;
esac
