#!/usr/bin/env bash
# 서버를 켜고(이미 켜져 있으면 그대로 두고) 브라우저로 앱을 엽니다.   bash termux/start.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
PORT="${PORT:-5000}"
DATA="${IGDL_DATA_DIR:-$HOME/.igdl}"
PIDFILE="$DATA/server.pid"

mkdir -p "$DATA"
chmod 700 "$DATA"

# 화면이 꺼져도 Android가 서버를 멈추지 않도록 (Termux 기본 제공 명령)
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "이미 실행 중이에요 (PID $(cat "$PIDFILE"))."
else
  # 기기 안(127.0.0.1)에서만 열립니다. 로그: $DATA/server.log
  HOST=127.0.0.1 PORT="$PORT" IGDL_DATA_DIR="$DATA" nohup "$PYTHON" app.py >"$DATA/server.log" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 2
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "서버가 시작되지 못했어요. 로그를 확인하세요:  cat $DATA/server.log" >&2
    rm -f "$PIDFILE"
    exit 1
  fi
  echo "서버를 켰어요 (PID $(cat "$PIDFILE"))."
fi

URL="http://localhost:$PORT"
if command -v termux-open-url >/dev/null 2>&1; then
  termux-open-url "$URL"
else
  echo "브라우저에서 $URL 을 여세요."
fi
