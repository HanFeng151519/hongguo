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

# 对白 ASR 首次需从 Hub 拉模型；国内网络可设 HONGGUO_HF_ENDPOINT 或 HF_ENDPOINT
if [ -n "${HONGGUO_HF_ENDPOINT:-}" ]; then
  export HF_ENDPOINT="$HONGGUO_HF_ENDPOINT"
elif [ "${HONGGUO_HF_MIRROR:-1}" != "0" ] && [ -z "${HF_ENDPOINT:-}" ]; then
  export HF_ENDPOINT="https://hf-mirror.com"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "未检测到 ffmpeg，钩子视频生成需要它。可执行: brew install ffmpeg"
fi

python3 -m pip install -r requirements.txt -q
if [ "${HONGGUO_ASR_ENABLED:-1}" != "0" ] && [ "${HONGGUO_ASR_ENABLED:-1}" != "false" ]; then
  python3 -m pip install -r requirements-asr.txt -q 2>/dev/null || \
    echo "提示: 对白 ASR 需安装依赖 → pip install -r server/requirements-asr.txt"
fi
if [ "${HONGGUO_FQ_KOC_AUTO_SYNC:-0}" = "1" ]; then
  python3 -m pip install -r requirements-browser.txt -q 2>/dev/null || \
    echo "提示: Playwright 需安装 → pip install -r server/requirements-browser.txt"
fi
echo ""
echo "请在浏览器打开: http://localhost:8000 （勿使用 http://0.0.0.0:8000）"
echo ""
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
