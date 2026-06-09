"""图片去水印：几何预设/自定义区域 + 平滑背景保色修复。"""

from __future__ import annotations

import os
from pathlib import Path


def _ext_for_output(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return ".jpg"
    if suffix in (".webp",):
        return ".webp"
    return ".png"


def _use_smooth_fill(preset: str) -> bool:
    v = os.getenv("HONGGUO_WM_IMAGE_SMOOTH_FILL", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    p = (preset or "").strip().lower()
    return p in ("douyin", "bottom-right", "bottom-left", "custom")


def remove_watermark_image_bytes(
    raw: bytes,
    filename: str,
    *,
    preset: str = "bottom-right",
    strength: str = "normal",
    x: int | None = None,
    y: int | None = None,
    w: int | None = None,
    h: int | None = None,
) -> tuple[bytes, str, dict]:
    import cv2
    import numpy as np

    import watermark_inpaint as wi
    from watermark_remove import (
        rect_from_custom,
        refine_douyin_rects,
        rects_for_preset_geometric,
        wm_strength,
    )

    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("无法读取图片")

    vh, vw = frame.shape[:2]
    strength = wm_strength(strength)
    if x is not None and y is not None and w is not None and h is not None:
        rects = [rect_from_custom(vw, vh, int(x), int(y), int(w), int(h))]
        mode = "custom"
    else:
        rects = rects_for_preset_geometric(vw, vh, preset, strength=strength)
        if preset.strip().lower() == "douyin":
            rects = refine_douyin_rects(frame, rects)
        mode = preset

    smooth = _use_smooth_fill(mode)
    dilate_px = (
        0
        if smooth
        else {"tight": 3, "normal": 4, "strong": 6}.get(strength, 4)
    )
    hard_mask = wi.build_mask(vh, vw, rects, dilate_px=dilate_px)
    erode_px = {"tight": 10, "normal": 8, "strong": 6}.get(strength, 8)
    core_mask = wi._core_mask(hard_mask, erode_px)  # type: ignore[attr-defined]
    feather_px = {"tight": 2, "normal": 3, "strong": 4}.get(strength, 3)
    method = "inpaint"

    fill_meta: dict = {}
    if smooth:
        if mode == "douyin":
            verts = wi.detect_douyin_vertices(frame)  # type: ignore[attr-defined]
            if verts:
                rects = [verts["rect"]]
            out, fill_meta = wi.fill_watermark_corner_color(frame, rects[0])  # type: ignore[attr-defined]
            method = "corner_fill"
        else:
            text_mask = wi.build_douyin_text_mask(frame, rects)  # type: ignore[attr-defined]
            if cv2.countNonZero(text_mask) < 8:
                text_mask = hard_mask
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            repair = cv2.dilate(text_mask, k, iterations=1)
            out = cv2.inpaint(frame, repair, 3, cv2.INPAINT_NS)
            out = wi._restore_grain(out, frame, repair)  # type: ignore[attr-defined]
            method = "inpaint_minimal"
    else:
        repair_mask = core_mask
        radius = 8 if strength == "strong" else 6
        inpainted = wi._inpaint_frame(frame, repair_mask, radius=radius)  # type: ignore[attr-defined]
        out = wi._finish_frame(
            frame,
            inpainted,
            repair_mask,
            feather_px=feather_px,
            core_mask=core_mask,
        )  # type: ignore[attr-defined]

    ext = _ext_for_output(filename)
    if ext == ".jpg":
        ok, encoded = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    elif ext == ".webp":
        ok, encoded = cv2.imencode(".webp", out, [int(cv2.IMWRITE_WEBP_QUALITY), 95])
    else:
        ok, encoded = cv2.imencode(".png", out)
    if not ok:
        raise RuntimeError("图片编码失败")

    meta = {
        "mode": mode,
        "method": method,
        "strength": strength,
        "width": vw,
        "height": vh,
        "regions": [{"x": rx, "y": ry, "w": rw, "h": rh} for rx, ry, rw, rh in rects],
    }
    if fill_meta:
        meta.update(fill_meta)
    return encoded.tobytes(), ext, meta
