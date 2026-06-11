#!/usr/bin/env bash
# 生成 / 下载 Real-ESRGAN AnimeVideoV3 Core ML 模型（与 Mac 后端 realesr-animevideov3 对齐）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
MODELS_DIR="$ROOT/ios/App/App/Models"
V3_PKG="$MODELS_DIR/RealESRGAN_v3.mlpackage"
LEGACY_MODEL="$MODELS_DIR/RealESRGAN.mlmodel"

if [ -d "$V3_PKG" ]; then
  echo "Real-ESRGAN v3 Core ML 模型已存在: $V3_PKG"
  exit 0
fi

mkdir -p "$MODELS_DIR"

CONVERT_PY="$SCRIPTS/convert-animevideov3-coreml.py"
VENV="$SCRIPTS/.venv-sr"

if [ ! -x "$VENV/bin/python" ]; then
  echo "创建转换环境…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$SCRIPTS/requirements-sr-convert.txt"
fi

echo "正在从 realesr-animevideov3 权重转换 Core ML（约 1–2 分钟）…"
if "$VENV/bin/python" "$CONVERT_PY" --models-dir "$MODELS_DIR"; then
  if [ -d "$V3_PKG" ]; then
    echo "已安装: $V3_PKG"
    if [ -f "$LEGACY_MODEL" ]; then
      echo "提示: 可删除旧版 x4plus 模型 $LEGACY_MODEL 以减小包体"
    fi
    exit 0
  fi
fi

cat >&2 <<'EOF'
AnimeVideoV3 Core ML 模型生成失败。请检查网络后重试：

  cd mobile && npm run ensure:sr-model

或手动执行：
  python3 -m venv mobile/scripts/.venv-sr
  mobile/scripts/.venv-sr/bin/pip install -r mobile/scripts/requirements-sr-convert.txt
  mobile/scripts/.venv-sr/bin/python mobile/scripts/convert-animevideov3-coreml.py

权重来源（国内镜像）：
  https://hf-mirror.com/leonelhs/realesrgan/resolve/main/realesr-animevideov3.pth
EOF
exit 1
