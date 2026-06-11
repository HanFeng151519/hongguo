#!/usr/bin/env python3
"""Convert realesr-animevideov3.pth to Core ML mlpackage for iOS."""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

TILE = 256
PTH_URLS = (
    "https://hf-mirror.com/leonelhs/realesrgan/resolve/main/realesr-animevideov3.pth",
    "https://huggingface.co/leonelhs/realesrgan/resolve/main/realesr-animevideov3.pth",
    "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
)


def download_pth(dest: Path) -> None:
    if dest.is_file() and dest.stat().st_size > 100_000:
        print(f"已有权重: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for url in PTH_URLS:
        try:
            print(f"下载 {url}")
            urllib.request.urlretrieve(url, dest)
            if dest.stat().st_size > 100_000:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"下载 realesr-animevideov3.pth 失败: {last_err}")


def convert(pth: Path, out_dir: Path, tile: int = TILE) -> Path:
    import coremltools as ct
    import torch
    from spandrel import ImageModelDescriptor, ModelLoader

    model = ModelLoader().load_from_file(str(pth))
    if not isinstance(model, ImageModelDescriptor):
        raise TypeError("期望 ImageModelDescriptor")
    scale = int(model.scale)
    torch_model = model.model
    torch_model.eval()
    torch_model.cpu()

    class CoreMLWrapper(torch.nn.Module):
        def __init__(self, inner: torch.nn.Module) -> None:
            super().__init__()
            self.inner = inner

        def forward(self, image: torch.Tensor) -> torch.Tensor:
            # image: NCHW float32 in [0, 1]
            out = self.inner(image)
            return torch.clamp(out, 0.0, 1.0)

    wrapped = CoreMLWrapper(torch_model)
    example = torch.rand(1, 3, tile, tile)
    with torch.no_grad():
        traced = torch.jit.trace(wrapped, example)

    mlmodel = ct.convert(
        traced,
        inputs=[ct.TensorType(name="input", shape=example.shape)],
        outputs=[ct.TensorType(name="output")],
        convert_to="mlprogram",
        minimum_deployment_target=ct.target.iOS15,
        compute_precision=ct.precision.FLOAT16,
    )
    mlmodel.author = "Real-ESRGAN"
    mlmodel.short_description = f"realesr-animevideov3 x{scale} tile {tile}"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "RealESRGAN_v3.mlpackage"
    if out_path.exists():
        import shutil

        shutil.rmtree(out_path)
    mlmodel.save(str(out_path))
    print(f"已生成 {out_path} (scale={scale}, tile={tile})")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "ios" / "App" / "App" / "Models",
    )
    parser.add_argument("--tile", type=int, default=TILE)
    args = parser.parse_args()

    models_dir = args.models_dir.resolve()
    cache_dir = models_dir / ".cache"
    pth = cache_dir / "realesr-animevideov3.pth"

    try:
        download_pth(pth)
        convert(pth, models_dir, tile=args.tile)
    except Exception as exc:  # noqa: BLE001
        print(f"转换失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
