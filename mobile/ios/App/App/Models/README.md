# Real-ESRGAN Core ML 模型

本机超分使用 **realesr-animevideov3**（与 Mac 后端默认模型一致），Core ML 包约 **1.2MB**。

在 `mobile/` 目录执行：

```bash
npm run ensure:sr-model
npm run cap:sync
```

生成文件：`RealESRGAN_v3.mlpackage`

权重来源：[xinntao/Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN)（BSD-3-Clause）
