"""本地 OCR 自动框选豆包等角标文字（标配，不依赖大模型）。"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ffmpeg_util import resolve_ffmpeg_exe

logger = logging.getLogger(__name__)

_OCR_ENGINE = None

# 豆包角标常见字样（子串匹配，忽略空格）
_WM_KEYWORD_RE = re.compile(
    r"豆包|doubao|ai\s*生成|ai生成|由\s*ai|generated|抖音|douyin|tiktok",
    re.IGNORECASE,
)

_CORNERS_FOR_PRESET: dict[str, tuple[str, ...]] = {
    "douyin": ("bottom-right",),
    "doubao": ("bottom-right", "top-left"),
    "top-left": ("top-left",),
    "bottom-right": ("bottom-right",),
    "bottom-left": ("bottom-left",),
    "top-right": ("top-right",),
    "all-corners": ("top-left", "top-right", "bottom-left", "bottom-right"),
    "bottom-bar": (),
}


def _even(n: int) -> int:
    return max(2, n - (n % 2))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _get_ocr():
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "去水印需要本地 OCR。请在 server 目录执行："
            "py -3.12 -m pip install rapidocr-onnxruntime"
        ) from exc
    _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _is_watermark_text(text: str) -> bool:
    t = (text or "").strip().replace(" ", "")
    if not t:
        return False
    if _WM_KEYWORD_RE.search(t):
        return True
    if "豆" in t and "包" in t:
        return True
    if re.search(r"ai", t, re.I) and "生成" in t:
        return True
    if "抖音" in t or re.search(r"douyin", t, re.I):
        return True
    if re.fullmatch(r"[@＠][\w\u4e00-\u9fff]{2,24}", t):
        return True
    # 角区内 OCR 常把「AI」「生成」拆成两段
    if len(t) <= 4 and re.fullmatch(r"(AI|ai|生成|aI|Ai)", t):
        return True
    return False


def _expand_rect_for_logo(
    rect: tuple[int, int, int, int],
    corner: str,
    vw: int,
    vh: int,
    *,
    preset: str = "",
) -> tuple[int, int, int, int]:
    """豆包图标在文字左侧，略向左上扩一圈；抖音角标更紧。"""
    x, y, w, h = rect
    c = corner.strip().lower()
    p = (preset or "").strip().lower()
    if p == "douyin":
        logo_w = min(x, max(12, int(vw * 0.028)))
        pad_y = max(2, int(h * 0.06))
    else:
        logo_w = min(x, max(28, int(vw * 0.055)))
        pad_y = max(4, int(h * 0.12))
    if c in ("bottom-right", "top-left", "bottom-left", "top-right"):
        x = max(0, x - logo_w)
        w = min(vw - x, w + logo_w)
        y = max(0, y - pad_y)
        h = min(vh - y, h + pad_y * 2)
    return _even(x), _even(y), _even(w), _even(h)


def _box_points_to_rect(
    box,
    *,
    offset_x: int,
    offset_y: int,
    frame_w: int,
    frame_h: int,
    pad: int,
) -> tuple[int, int, int, int]:
    import numpy as np

    pts = np.asarray(box, dtype=float)
    xs = pts[:, 0] + offset_x
    ys = pts[:, 1] + offset_y
    x = int(max(0, np.floor(xs.min()) - pad))
    y = int(max(0, np.floor(ys.min()) - pad))
    x2 = int(min(frame_w, np.ceil(xs.max()) + pad))
    y2 = int(min(frame_h, np.ceil(ys.max()) + pad))
    w = _even(x2 - x)
    h = _even(y2 - y)
    return x, y, w, h


def _corner_scan_roi(
    corner: str,
    vw: int,
    vh: int,
) -> tuple[int, int, int, int]:
    """角区搜索范围（像素）。"""
    c = corner.strip().lower()
    if c == "top-left":
        return 0, 0, _even(int(vw * 0.58)), _even(int(vh * 0.22))
    if c == "top-right":
        return _even(int(vw * 0.42)), 0, _even(vw - int(vw * 0.42)), _even(int(vh * 0.22))
    if c == "bottom-left":
        return 0, _even(int(vh * 0.68)), _even(int(vw * 0.58)), _even(vh - int(vh * 0.68))
    # bottom-right
    return _even(int(vw * 0.38)), _even(int(vh * 0.62)), _even(vw - int(vw * 0.38)), _even(vh - int(vh * 0.62))


def _extract_frame(src: Path, t: float, dest: Path) -> bool:
    ffmpeg = resolve_ffmpeg_exe()
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, t):.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            str(dest),
        ],
        capture_output=True,
        timeout=60,
    )
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 100


def _ocr_rects_in_roi(
    image_path: Path,
    roi: tuple[int, int, int, int],
    vw: int,
    vh: int,
    *,
    min_score: float,
    pad: int,
) -> list[tuple[int, int, int, int]]:
    import numpy as np
    from PIL import Image

    rx, ry, rw, rh = roi
    img = Image.open(image_path).convert("RGB")
    crop = img.crop((rx, ry, rx + rw, ry + rh))
    arr = np.array(crop)

    ocr = _get_ocr()
    result, _ = ocr(arr)
    if not result:
        return []

    rects: list[tuple[int, int, int, int]] = []
    for item in result:
        if len(item) < 3:
            continue
        box, text, score = item[0], item[1], float(item[2])
        if score < min_score:
            continue
        if not _is_watermark_text(str(text)):
            continue
        rects.append(
            _box_points_to_rect(
                box,
                offset_x=rx,
                offset_y=ry,
                frame_w=vw,
                frame_h=vh,
                pad=pad,
            )
        )
    return rects


def _union_rect(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    vw: int,
    vh: int,
) -> tuple[int, int, int, int]:
    x = min(a[0], b[0])
    y = min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    x = max(0, x)
    y = max(0, y)
    x2 = min(vw, x2)
    y2 = min(vh, y2)
    return _even(x), _even(y), _even(x2 - x), _even(y2 - y)


def _merge_rects(
    rects: list[tuple[int, int, int, int]],
    vw: int,
    vh: int,
    *,
    extra_pad: int = 0,
) -> tuple[int, int, int, int] | None:
    if not rects:
        return None
    x = min(r[0] for r in rects)
    y = min(r[1] for r in rects)
    x2 = max(r[0] + r[2] for r in rects)
    y2 = max(r[1] + r[3] for r in rects)
    p = max(0, extra_pad)
    x = max(0, x - p)
    y = max(0, y - p)
    x2 = min(vw, x2 + p)
    y2 = min(vh, y2 + p)
    return _even(x), _even(y), _even(x2 - x), _even(y2 - y)


def _sample_times(duration: float) -> list[float]:
    if duration <= 0:
        return [0.0]
    n = max(3, min(8, _env_int("HONGGUO_WM_OCR_SAMPLES", 5)))
    if duration < 0.5:
        return [0.0]
    step = duration / max(1, n - 1)
    return [min(duration, i * step) for i in range(n)]


def rects_from_ocr_video(
    src: Path,
    vw: int,
    vh: int,
    preset: str,
    *,
    duration: float,
    strength: str | None = None,
    geometric_fallback,
) -> list[tuple[int, int, int, int]]:
    """
    多帧 OCR 合并角标框；某角未识别到时用 geometric_fallback 同序矩形。
    geometric_fallback: Callable 与 watermark_remove.rects_for_preset_geometric 一致。
    """
    p = (preset or "bottom-right").strip().lower()
    corners = _CORNERS_FOR_PRESET.get(p)
    if corners is None:
        corners = ("bottom-right",)

    if p == "bottom-bar":
        return geometric_fallback(vw, vh, preset, strength=strength)

    min_score = _env_float("HONGGUO_WM_OCR_MIN_SCORE", 0.35)
    pad = _env_int(
        "HONGGUO_WM_OCR_PAD",
        6 if p == "douyin" else 10,
    )
    times = _sample_times(duration)

    fallback_rects = geometric_fallback(vw, vh, preset, strength=strength)
    fb_map = dict(zip(corners, fallback_rects[: len(corners)]))

    collected: dict[str, list[tuple[int, int, int, int]]] = {c: [] for c in corners}
    td = Path(tempfile.gettempdir())

    for t in times:
        frame = td / f"wm_ocr_{os.getpid()}_{int(t * 1000)}.png"
        try:
            if not _extract_frame(src, t, frame):
                continue
            for corner in corners:
                roi = _corner_scan_roi(corner, vw, vh)
                for rect in _ocr_rects_in_roi(
                    frame, roi, vw, vh, min_score=min_score, pad=0
                ):
                    collected[corner].append(rect)
        finally:
            frame.unlink(missing_ok=True)

    out: list[tuple[int, int, int, int]] = []
    for corner in corners:
        merged = _merge_rects(collected[corner], vw, vh, extra_pad=pad)
        if merged:
            merged = _expand_rect_for_logo(merged, corner, vw, vh, preset=p)
            logger.info("OCR 水印框 %s: %s", corner, merged)
            out.append(merged)
        elif corner in fb_map:
            logger.info("OCR 未命中 %s，使用几何预设 %s", corner, fb_map[corner])
            out.append(fb_map[corner])
        else:
            out.append(fb_map.get(corner, (0, 0, vw // 4, vh // 10)))

    return out
