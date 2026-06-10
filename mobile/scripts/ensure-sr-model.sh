#!/usr/bin/env bash
# 下载 Real-ESRGAN Core ML 模型（GitHub Release 已失效，改用 Hugging Face）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODELS_DIR="$ROOT/ios/App/App/Models"
MLMODEL="$MODELS_DIR/RealESRGAN.mlmodel"
MLPACKAGE="$MODELS_DIR/RealESRGAN_x2plus.mlpackage"

if [ -f "$MLMODEL" ] || [ -d "$MLPACKAGE" ]; then
  echo "Real-ESRGAN Core ML 模型已存在"
  exit 0
fi

mkdir -p "$MODELS_DIR"
TMP_ZIP="$(mktemp -t realesrgan.XXXXXX.zip)"

download_one() {
  local url="$1"
  echo "尝试下载: $url"
  if curl -fsSL -L --connect-timeout 30 --retry 2 --retry-delay 2 "$url" -o "$TMP_ZIP"; then
    return 0
  fi
  return 1
}

# 原 marshiyar GitHub Release 已 404；可用源如下（国内优先 hf-mirror）
URLS=(
  "https://hf-mirror.com/mszpro/CoreML_RealESRGAN/resolve/main/RealESRGAN.mlmodel.zip"
  "https://huggingface.co/mszpro/CoreML_RealESRGAN/resolve/main/RealESRGAN.mlmodel.zip"
)

ok=0
for url in "${URLS[@]}"; do
  if download_one "$url"; then
    ok=1
    break
  fi
  echo "下载失败，换下一个镜像…"
done

if [ "$ok" -ne 1 ]; then
  rm -f "$TMP_ZIP"
  cat >&2 <<'EOF'
所有自动下载均失败。请手动操作：

1. 浏览器打开（国内推荐镜像）：
   https://hf-mirror.com/mszpro/CoreML_RealESRGAN/tree/main
   或国际站：
   https://huggingface.co/mszpro/CoreML_RealESRGAN/tree/main

2. 下载 RealESRGAN.mlmodel.zip（约 64MB）

3. 解压后将 RealESRGAN.mlmodel 放到：
   mobile/ios/App/App/Models/RealESRGAN.mlmodel

4. 重新执行 npm run cap:sync

也可在 App 内首次点「AI 超分」时自动下载（需联网）。
EOF
  exit 1
fi

unzip -q -o -j "$TMP_ZIP" "RealESRGAN.mlmodel" -d "$MODELS_DIR" 2>/dev/null || unzip -q -o "$TMP_ZIP" -d "$MODELS_DIR"
rm -f "$TMP_ZIP"

if [ ! -f "$MLMODEL" ]; then
  FOUND="$(find "$MODELS_DIR" -maxdepth 2 -name 'RealESRGAN.mlmodel' -type f | head -1)"
  if [ -n "$FOUND" ] && [ "$FOUND" != "$MLMODEL" ]; then
    mv "$FOUND" "$MLMODEL"
  fi
fi

if [ ! -f "$MLMODEL" ] && [ ! -d "$MLPACKAGE" ]; then
  echo "解压失败，未找到 RealESRGAN.mlmodel" >&2
  exit 1
fi

echo "已安装: ${MLMODEL:-$MLPACKAGE}"
