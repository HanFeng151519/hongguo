#!/usr/bin/env bash
# 生成 iOS Core ML 超分模型
#   general（默认）= realesr-general-x4v3  真人均衡，比 v3 自然、比 x4plus 快
#   anime          = realesr-animevideov3    动漫优化
# 用法：HONGGUO_SR_IOS_MODEL=anime npm run ensure:sr-model
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$ROOT/scripts"
MODELS_DIR="$ROOT/ios/App/App/Models"
APP_ROOT="$ROOT/ios/App/App"
IOS_MODEL="${HONGGUO_SR_IOS_MODEL:-general}"
CONVERT_PY="$SCRIPTS/convert-sr-coreml.py"
VENV="$SCRIPTS/.venv-sr"

case "$IOS_MODEL" in
  anime)
    PKG_NAME="RealESRGAN_v3.mlpackage"
    ;;
  general|real|balanced)
    IOS_MODEL="general"
    PKG_NAME="RealESRGAN_general.mlpackage"
    ;;
  *)
    echo "未知 HONGGUO_SR_IOS_MODEL=$IOS_MODEL（可用 general / anime）" >&2
    exit 1
    ;;
esac

PKG_PATH="$MODELS_DIR/$PKG_NAME"

if [ -d "$PKG_PATH" ]; then
  echo "Core ML 模型已存在: $PKG_PATH"
  if [ ! -d "$APP_ROOT/$PKG_NAME" ]; then
    cp -R "$PKG_PATH" "$APP_ROOT/"
    echo "已复制到: $APP_ROOT/$PKG_NAME"
  fi
  exit 0
fi

mkdir -p "$MODELS_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "创建转换环境…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$SCRIPTS/requirements-sr-convert.txt"
fi

echo "正在转换 $IOS_MODEL 模型为 Core ML（约 1–3 分钟）…"
if "$VENV/bin/python" "$CONVERT_PY" --model "$IOS_MODEL" --models-dir "$MODELS_DIR"; then
  if [ -d "$PKG_PATH" ]; then
    cp -R "$PKG_PATH" "$APP_ROOT/" 2>/dev/null || true
    echo "已安装: $PKG_PATH"
    exit 0
  fi
fi

cat >&2 <<EOF
Core ML 模型生成失败。请检查网络后重试：

  cd mobile && npm run ensure:sr-model

或指定动漫模型：
  HONGGUO_SR_IOS_MODEL=anime npm run ensure:sr-model
EOF
exit 1
