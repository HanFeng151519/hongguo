#!/bin/bash
set -e
ROOT="$(dirname "$0")"
cd "$ROOT/server"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "未检测到 ffmpeg，钩子视频生成需要它。可执行: brew install ffmpeg"
fi

python3 -m pip install -r requirements.txt -q
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
