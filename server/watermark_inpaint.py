"""OpenCV 去水印：逐帧 inpaint，遮罩内全替换，外缘宽羽化（无贴块）。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401

        return True
    except ImportError:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def build_mask(
    height: int,
    width: int,
    rects: list[tuple[int, int, int, int]],
    *,
    dilate_px: int = 5,
):
    import numpy as np

    mask = np.zeros((height, width), dtype=np.uint8)
    for x, y, w, h in rects:
        x2 = min(width, x + w)
        y2 = min(height, y + h)
        if x2 > x and y2 > y:
            mask[y:y2, x:x2] = 255
    if dilate_px > 0:
        import cv2

        k = max(1, dilate_px)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.dilate(mask, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def _core_mask(hard_mask, erode_px: int):
    """文字核心（略小于遮罩），用于消字，不整块替换。"""
    import cv2

    if erode_px <= 0:
        return hard_mask
    k = max(3, erode_px)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    core = cv2.erode(hard_mask, kernel, iterations=1)
    if cv2.countNonZero(core) < 16:
        return hard_mask
    return core


def _feather_mask(mask, feather_px: int):
    import cv2

    if feather_px <= 0:
        return mask
    k = feather_px * 2 + 1
    return cv2.GaussianBlur(mask, (k, k), 0)


def _inpaint_frame(frame, mask, *, radius: int):
    import cv2

    r = max(3, radius)
    out = cv2.inpaint(frame, mask, r, cv2.INPAINT_TELEA)
    out = cv2.inpaint(out, mask, max(3, r // 2), cv2.INPAINT_NS)
    return out


def _ring_mask(hard_mask, ring_px: int):
    import cv2

    ring_px = max(4, ring_px)
    k_out = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ring_px * 2 + 1, ring_px * 2 + 1))
    k_in = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    outer = cv2.dilate(hard_mask, k_out)
    inner = cv2.erode(hard_mask, k_in)
    return cv2.subtract(outer, inner)


def _color_harmonize(repaired, reference, hard_mask, *, ring_px: int):
    import cv2
    import numpy as np

    ring = _ring_mask(hard_mask, ring_px)
    if cv2.countNonZero(ring) < 24 or cv2.countNonZero(hard_mask) < 12:
        return repaired

    rep_lab = cv2.cvtColor(repaired, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(np.float32)
    core = hard_mask > 0
    ring_b = ring > 0

    for c in range(3):
        ref_mean = float(ref_lab[:, :, c][ring_b].mean())
        ref_std = float(ref_lab[:, :, c][ring_b].std()) + 1e-6
        rep_mean = float(rep_lab[:, :, c][core].mean())
        rep_std = float(rep_lab[:, :, c][core].std()) + 1e-6
        channel = rep_lab[:, :, c].copy()
        channel[core] = (channel[core] - rep_mean) * (ref_std / rep_std) + ref_mean
        rep_lab[:, :, c] = channel

    return cv2.cvtColor(np.clip(rep_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


def _outward_alpha(hard_mask, feather_px: int):
    """
    遮罩内 alpha=1（全用 inpaint）；
    仅在遮罩外缘做宽羽化，避免贴块 / 画中画感。
    """
    import cv2
    import numpy as np

    inside = (hard_mask > 0).astype(np.float32)
    if feather_px <= 0:
        return inside

    k = feather_px * 2 + 1
    expanded = cv2.dilate(
        hard_mask,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)),
    )
    soft = cv2.GaussianBlur(expanded.astype(np.float32), (k, k), 0) / 255.0
    return np.maximum(soft, inside)


def _touch_core_text(repaired, core_mask, *, sigma: float):
    """仅在文字核心对修复结果轻模糊，消残留笔画，不贴新块。"""
    import cv2
    import numpy as np

    if sigma <= 0 or cv2.countNonZero(core_mask) < 8:
        return repaired
    soft = cv2.GaussianBlur(repaired, (0, 0), sigma)
    alpha = (core_mask.astype(np.float32) / 255.0)[:, :, None]
    out = repaired.astype(np.float32) * (1.0 - alpha) + soft.astype(np.float32) * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def _finish_frame(frame, inpainted, hard_mask, *, feather_px: int, core_mask):
    import numpy as np

    repaired = inpainted
    if _env_int("HONGGUO_WM_INPAINT_COLOR_MATCH", 1):
        ring_px = _env_int("HONGGUO_WM_INPAINT_RING", 10)
        repaired = _color_harmonize(repaired, frame, hard_mask, ring_px=ring_px)

    alpha = _outward_alpha(hard_mask, feather_px)
    alpha3 = np.stack([alpha, alpha, alpha], axis=-1)
    base = frame.astype(np.float32)
    over = repaired.astype(np.float32)
    out = base * (1.0 - alpha3) + over * alpha3
    return np.clip(out, 0, 255).astype(np.uint8)


def inpaint_video(
    src: Path,
    dest: Path,
    rects: list[tuple[int, int, int, int]],
    *,
    radius: int = 5,
    dilate_px: int = 5,
    strength: str = "normal",
) -> None:
    import cv2

    if not rects:
        raise RuntimeError("未指定水印区域")

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError("无法打开视频（OpenCV）")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
    if fps < 1:
        fps = 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width < 2 or height < 2:
        cap.release()
        raise RuntimeError("无法读取视频分辨率")

    s_key = (strength or "normal").strip().lower()
    hard_mask = build_mask(height, width, rects, dilate_px=dilate_px)
    core_mask = _core_mask(hard_mask, _env_int("HONGGUO_WM_CORE_ERODE", 6 if s_key == "strong" else 4))
    feather_px = _env_int("HONGGUO_WM_INPAINT_FEATHER", 16 if s_key == "strong" else 14)
    core_sigma = {"tight": 0.0, "normal": 2.5, "strong": 4.0}.get(s_key, 2.5)

    tmp = dest.with_suffix(".cv.mp4")
    tmp.unlink(missing_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp), fourcc, fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError("无法创建临时视频（OpenCV VideoWriter）")

    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        inp = _inpaint_frame(frame, hard_mask, radius=radius)
        inp = _touch_core_text(inp, core_mask, sigma=core_sigma)
        frame = _finish_frame(frame, inp, hard_mask, feather_px=feather_px, core_mask=core_mask)
        writer.write(frame)
        frames += 1

    cap.release()
    writer.release()

    if frames < 1 or not tmp.is_file() or tmp.stat().st_size < 10_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("修复后视频为空")

    logger.info(
        "OpenCV inpaint %s frames %dx%d r=%d dilate=%d feather=%d strength=%s",
        frames,
        width,
        height,
        radius,
        dilate_px,
        feather_px,
        s_key,
    )
    dest.unlink(missing_ok=True)
    tmp.replace(dest)
