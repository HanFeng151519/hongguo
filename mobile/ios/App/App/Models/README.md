# Real-ESRGAN Core ML 模型

| 档位 | 权重 | Core ML 包 | 适用 |
|------|------|------------|------|
| **真人推荐**（默认） | `realesr-general-x4v3` | `RealESRGAN_general.mlpackage` (~2–3MB) | 真人短剧，速度与画质折中 |
| **动漫优化** | `realesr-animevideov3` | `RealESRGAN_v3.mlpackage` (~1.2MB) | 动漫/二次元，最快 |

在 `mobile/` 目录执行：

```bash
# 真人均衡（默认）
npm run ensure:sr-model

# 或只要动漫 v3
HONGGUO_SR_IOS_MODEL=anime npm run ensure:sr-model

npm run cap:sync
```

Mac 后端「真人推荐」同样默认 `realesr-general-x4v3`（首次超分会自动下载 ncnn 权重）。
