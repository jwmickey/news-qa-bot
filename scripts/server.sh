#!/bin/bash
# Start/stop the web UI and the LanguageTool server behind it.
#
#   scripts/server.sh start|stop|restart|status|logs
#
# uvicorn runs without --reload, so code changes need a restart. The
# LanguageTool server is a separate Java process that language_tool_python
# spawns on the first scan; it outlives the web process, so stop kills it too.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
DATA_DIR="${NEWS_QA_DATA_DIR:-${PROJECT_DIR}/data}"
PID_FILE="${DATA_DIR}/web.pid"
LOG_FILE="${DATA_DIR}/web.log"
HOST="${NEWS_QA_WEB_HOST:-127.0.0.1}"
PORT="${NEWS_QA_WEB_PORT:-8000}"

# The jar path is how we tell our LanguageTool server from any other java.
LT_PATTERN='languagetool-server.jar'

cd "$PROJECT_DIR"

# Echo the web server's pid, or nothing. Prefers the pid file but falls back to
# the listening socket, so a server started by hand is still manageable.
web_pid() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return
        fi
        rm -f "$PID_FILE"
    fi
    lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true
}

lt_pids() {
    pgrep -f "$LT_PATTERN" 2>/dev/null || true
}

# SIGTERM, then SIGKILL if it is still around after `timeout` seconds.
stop_pid() {
    local pid="$1" timeout="${2:-10}" waited=0
    kill "$pid" 2>/dev/null || return 0
    while kill -0 "$pid" 2>/dev/null; do
        if (( waited >= timeout )); then
            kill -9 "$pid" 2>/dev/null || true
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
}

cmd_start() {
    local existing
    existing="$(web_pid)"
    if [[ -n "$existing" ]]; then
        echo "already running (pid ${existing})  →  http://${HOST}:${PORT}"
        return 0
    fi

    mkdir -p "$DATA_DIR"
    nohup "$PYTHON" -m news_qa.web >>"$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" >"$PID_FILE"

    # Confirm it survived startup rather than reporting a pid that already died.
    sleep 2
    if ! kill -0 "$pid" 2>/dev/null; then
        rm -f "$PID_FILE"
        echo "failed to start; last lines of ${LOG_FILE}:" >&2
        tail -n 20 "$LOG_FILE" >&2
        return 1
    fi

    echo "started (pid ${pid})  →  http://${HOST}:${PORT}"
    echo "logs: ${LOG_FILE}"
}

cmd_stop() {
    local pid
    pid="$(web_pid)"
    if [[ -n "$pid" ]]; then
        stop_pid "$pid"
        echo "stopped web server (pid ${pid})"
    else
        echo "web server not running"
    fi
    rm -f "$PID_FILE"

    local lt
    lt="$(lt_pids)"
    if [[ -n "$lt" ]]; then
        for p in $lt; do
            stop_pid "$p"
        done
        echo "stopped LanguageTool ($(echo "$lt" | tr '\n' ' ' | sed 's/ $//'))"
    fi
}

cmd_status() {
    local pid lt
    pid="$(web_pid)"
    if [[ -n "$pid" ]]; then
        echo "web:          running (pid ${pid})  http://${HOST}:${PORT}"
    else
        echo "web:          stopped"
    fi

    lt="$(lt_pids)"
    if [[ -n "$lt" ]]; then
        echo "languagetool: running (pid $(echo "$lt" | tr '\n' ' ' | sed 's/ $//'))"
    else
        echo "languagetool: stopped (starts on the next scan)"
    fi
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)  cmd_status ;;
    logs)    tail -f "$LOG_FILE" ;;
    *)
        echo "usage: $(basename "$0") {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac
