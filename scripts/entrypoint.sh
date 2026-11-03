#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/challenges"

# 优先使用 GUI 写入的密码文件
if [[ -f "$DATA_DIR/vnc_password" ]]; then
  VNC_PASSWORD="$(tr -d '\r\n' < "$DATA_DIR/vnc_password")"
  export VNC_PASSWORD
fi

# 配置了 Basic Auth 时，VNC 本机无密码，对外由 /vnc 代理鉴权
if [[ -n "${BASIC_AUTH_USER:-}" && -n "${BASIC_AUTH_PASSWORD:-}" ]]; then
  VNC_PASSWORD=""
  export VNC_PASSWORD
  echo "[entrypoint] Basic Auth enabled → x11vnc without password (gate via /vnc)"
fi

cleanup() {
  echo "[entrypoint] shutting down..."
  kill "$WORKER_PID" "$API_PID" 2>/dev/null || true
  if [[ -n "${VNC_PIDS:-}" ]]; then
    # shellcheck disable=SC2086
    kill $VNC_PIDS 2>/dev/null || true
  fi
  wait || true
}
trap cleanup EXIT INT TERM

start_vnc() {
  echo "[entrypoint] starting Xvfb on $DISPLAY"
  Xvfb "$DISPLAY" -screen 0 1280x800x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
  VNC_PIDS="$!"
  sleep 1

  VNC_PASS_ARGS=()
  if [[ -n "${VNC_PASSWORD:-}" ]]; then
    mkdir -p /tmp/vnc
    x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vnc/passwd >/dev/null
    VNC_PASS_ARGS=(-rfbauth /tmp/vnc/passwd)
    echo "[entrypoint] x11vnc password enabled"
  else
    VNC_PASS_ARGS=(-nopw)
    echo "[entrypoint] x11vnc without password"
  fi

  echo "[entrypoint] starting x11vnc :5900"
  x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 "${VNC_PASS_ARGS[@]}" >/tmp/x11vnc.log 2>&1 &
  VNC_PIDS="$VNC_PIDS $!"

  # 自定义 noVNC 落地页（可被 /data/novnc_index.html 覆盖）
  CUSTOM_NOVNC="${DATA_DIR}/novnc_index.html"
  if [[ -f "$CUSTOM_NOVNC" ]]; then
    mkdir -p /tmp/novnc-web
    cp -a /usr/share/novnc/. /tmp/novnc-web/
    cp "$CUSTOM_NOVNC" /tmp/novnc-web/index.html
    NOVNC_WEB=/tmp/novnc-web
    echo "[entrypoint] using custom noVNC page from $CUSTOM_NOVNC"
  else
    NOVNC_WEB=/usr/share/novnc
  fi

  echo "[entrypoint] starting noVNC :7900 (internal)"
  websockify --web="$NOVNC_WEB" 7900 localhost:5900 >/tmp/novnc.log 2>&1 &
  VNC_PIDS="$VNC_PIDS $!"
}

if [[ "${ENABLE_VNC:-false}" == "true" ]]; then
  export PLAYWRIGHT_HEADLESS=false
  export ENABLE_VNC=true
  start_vnc
fi

echo "[entrypoint] starting login_worker"
python login_worker.py &
WORKER_PID=$!

echo "[entrypoint] starting API on ${API_PORT:-8056}"
python main.py &
API_PID=$!

wait -n "$WORKER_PID" "$API_PID" || true
cleanup
