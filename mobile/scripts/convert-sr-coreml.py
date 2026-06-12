#!/usr/bin/env python3
"""Convert Real-ESRGAN SRVGG weights to Core ML mlpackage for iOS."""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

MODELS = {
    "anime": {
        "pth": "realesr-animevideov3.pth",
        "out": "RealESRGAN_v3.mlpackage",
        "desc": "realesr-animevideov3",
        "urls": (
            "https://hf-mirror.com/leonelhs/realesrgan/resolve/main/realesr-animevideov3.pth",
            "https://huggingface.co/leonelhs/realesrgan/resolve/main/realesr-animevideov3.pth",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
        ),
    },
    "general": {
        "pth": "realesr-general-x4v3.pth",
        "out": "RealESRGAN_general.mlpackage",
        "desc": "realesr-general-x4v3",
        "urls": (
            "https://hf-mirror.com/leonelhs/realesrgan/resolve/main/realesr-general-x4v3.pth",
            "https://huggingface.co/leonelhs/realesrgan/resolve/main/realesr-general-x4v3.pth",
            "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
        ),
    },
}


def download_pth(model_key: str, cache_dir: Path) -> Path:
    spec = MODELS[model_key]
    dest = cache_dir / spec["pth"]
    if dest.is_file() and dest.stat().st_size > 100_000:
        print(f"已有权重: {dest}")
        return dest
    cache_dir.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for url in spec["urls"]:
        try:
            print(f"下载 {url}")
            urllib.request.urlretrieve(url, dest)
            if dest.stat().st_size > 100_000:
                return dest
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"下载 {spec['pth']} 失败: {last_err}")


def convert_model(
    pth: Path,
    out_dir: Path,
    *,
    model_key: str,
    tile: int = 256,
) -> Path:
    import coremltools as ct
    import torch
    from spandrel import ImageModelDescriptor, ModelLoader

    spec = MODELS[model_key]
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
            return torch.clamp(self.inner(image), 0.0, 1.0)

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
    mlmodel.short_description = f"{spec['desc']} x{scale} tile {tile}"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / spec["out"]
    if out_path.exists():
        import shutil

        shutil.rmtree(out_path)
    mlmodel.save(str(out_path))
    print(f"已生成 {out_path} (scale={scale}, tile={tile})")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        choices=tuple(MODELS.keys()),
        default="general",
        help="anime=realesr-animevideov3, general=真人折中 realesr-general-x4v3",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "ios" / "App" / "App" / "Models",
    )
    parser.add_argument("--tile", type=int, default=256)
    args = parser.parse_args()

    models_dir = args.models_dir.resolve()
    cache_dir = models_dir / ".cache"

    try:
        pth = download_pth(args.model, cache_dir)
        convert_model(pth, models_dir, model_key=args.model, tile=args.tile)
    except Exception as exc:  # noqa: BLE001
        print(f"转换失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
