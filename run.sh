#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

uv run python login_worker.py &
WORKER_PID=$!

cleanup() {
  kill "$WORKER_PID" 2>/dev/null || true
  wait "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run python main.py
