#!/usr/bin/env bash
# 서버를 끕니다.   bash termux/stop.sh
set -euo pipefail

DATA="${IGDL_DATA_DIR:-$HOME/.igdl}"
PIDFILE="$DATA/server.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  kill "$(cat "$PIDFILE")"
  echo "서버를 껐어요."
else
  echo "실행 중인 서버가 없어요."
fi
rm -f "$PIDFILE"
command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock || true
