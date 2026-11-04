#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/challenges"

# 模拟慢网：NET_RATE / NET_DELAY / NET_LOSS（需 CAP_NET_ADMIN）
# 例：NET_RATE=2mbit NET_DELAY=100ms NET_LOSS=1%
# 留空或 NET_RATE=off 关闭
apply_net_throttle() {
  local rate="${NET_RATE:-}"
  if [[ -z "$rate" || "$rate" == "0" || "$rate" == "off" ]]; then
    echo "[entrypoint] net throttle disabled"
    return 0
  fi
  if ! command -v tc >/dev/null 2>&1; then
    echo "[entrypoint] tc not found (install iproute2); skip net throttle" >&2
    return 0
  fi
  local iface
  iface="$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')"
  if [[ -z "$iface" ]]; then
    iface="$(ip -o link show up 2>/dev/null | awk -F': ' '$2 !~ /lo/ {print $2; exit}')"
  fi
  if [[ -z "$iface" ]]; then
    echo "[entrypoint] no network iface; skip net throttle" >&2
    return 0
  fi
  local delay="${NET_DELAY:-}"
  local loss="${NET_LOSS:-}"
  local args=(rate "$rate")
  [[ -n "$delay" && "$delay" != "0" && "$delay" != "off" ]] && args+=(delay "$delay")
  [[ -n "$loss" && "$loss" != "0" && "$loss" != "off" && "$loss" != "0%" ]] && args+=(loss "$loss")

  tc qdisc del dev "$iface" root 2>/dev/null || true
  if tc qdisc add dev "$iface" root netem "${args[@]}"; then
    echo "[entrypoint] net throttle on $iface: ${args[*]}"
  else
    echo "[entrypoint] failed to apply tc netem (need cap_add: NET_ADMIN?)" >&2
  fi
}

apply_net_throttle

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
  # 与 browser.py _VNC_SCREEN 一致；浏览器默认铺满
  echo "[entrypoint] starting Xvfb on $DISPLAY 1280x800"
  Xvfb "$DISPLAY" -screen 0 1280x800x24 -dpi 96 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
  VNC_PIDS="$!"
  sleep 1

  # x11vnc 仅容器内访问；对外由 /vnc 代理 + 控制台登录 Cookie 鉴权
  echo "[entrypoint] starting x11vnc :5900 (nopw, gated via /vnc)"
  x11vnc -display "$DISPLAY" -forever -shared -rfbport 5900 -nopw >/tmp/x11vnc.log 2>&1 &
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
