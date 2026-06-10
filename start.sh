#!/bin/bash
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
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

VENV_PY="$ROOT/server/.venv/bin/python"

pick_bootstrap_python() {
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

if [ ! -x "$VENV_PY" ]; then
  BOOT="$(pick_bootstrap_python || true)"
  if [ -z "$BOOT" ]; then
    echo "错误：需要 Python 3.10+。可执行: brew install python@3.12"
    echo "然后: cd $ROOT/server && python3.12 -m venv .venv"
    exit 1
  fi
  echo "正在创建虚拟环境 server/.venv（$("$BOOT" --version 2>&1)）…"
  "$BOOT" -m venv "$ROOT/server/.venv"
fi

PY="$VENV_PY"
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "错误：server/.venv 版本过低（$("$PY" --version 2>&1)），需要 Python 3.10+。"
  echo "请执行: rm -rf $ROOT/server/.venv && ./start.sh"
  exit 1
fi

"$PY" -m pip install -r requirements.txt -q
if [ "${HONGGUO_ASR_ENABLED:-1}" != "0" ] && [ "${HONGGUO_ASR_ENABLED:-1}" != "false" ]; then
  "$PY" -m pip install -r requirements-asr.txt -q 2>/dev/null || \
    echo "提示: 对白 ASR 需安装依赖 → pip install -r server/requirements-asr.txt"
fi
if [ "${HONGGUO_FQ_KOC_AUTO_SYNC:-0}" = "1" ]; then
  "$PY" -m pip install -r requirements-browser.txt -q 2>/dev/null || \
    echo "提示: Playwright 需安装 → pip install -r server/requirements-browser.txt"
fi
echo ""
echo "请在浏览器打开: http://localhost:8000 （勿使用 http://0.0.0.0:8000）"
echo "Python: $("$PY" --version 2>&1)  (server/.venv)"
echo ""
"$PY" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
