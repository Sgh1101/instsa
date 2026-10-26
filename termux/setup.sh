#!/usr/bin/env bash
# 안드로이드 Termux에서 한 번만 실행:  bash termux/setup.sh
set -euo pipefail

if ! command -v pkg >/dev/null 2>&1; then
  echo "Termux 안에서 실행해 주세요. (PC라면 README의 '실행'을 따르세요.)" >&2
  exit 1
fi

cd "$(dirname "$0")/.."

echo "[1/3] 패키지 설치 (python, ffmpeg)"
pkg update -y
pkg install -y python ffmpeg

echo "[2/3] 파이썬 환경 만들기"
python -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[3/3] 실행 파일 권한 설정"
chmod +x termux/*.sh

cat <<'MSG'

설치가 끝났어요. 이제:
  bash termux/start.sh     # 서버를 켜고 Chrome으로 앱을 엽니다
Chrome 메뉴(⋮) → '앱 설치' 또는 '홈 화면에 추가'를 눌러 앱처럼 쓰세요.
MSG
