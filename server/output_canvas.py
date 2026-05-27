"""成片输出画幅：默认跟源片像素（auto/native）；可选标准 1080×1920 / 1920×1080。"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PORTRAIT_SIZE = (1080, 1920)
LANDSCAPE_SIZE = (1920, 1080)


@dataclass(frozen=True)
class CanvasSpec:
    width: int
    height: int
    label: str


def _even_px(n: int) -> int:
    v = max(2, int(n))
    return v if v % 2 == 0 else v + 1


def _label_for(width: int, height: int) -> str:
    if height > width * 1.05:
        return "9:16"
    if width > height * 1.05:
        return "16:9"
    return f"{width}x{height}"


def _use_native_source_size() -> bool:
    mode = output_aspect_mode()
    if mode in ("native", "source", "原始", "原画", "原尺寸"):
        return True
    if mode in ("standard", "douyin", "平台", "1080"):
        return False
    # auto：跟源片分辨率，不强行拉到 1080p/横屏
    return mode in ("auto", "")


def canvas_from_source_size(source_w: int, source_h: int) -> CanvasSpec:
    """native/auto：源片多大成片多大（仅取偶数像素）；standard：平台标准分辨率。"""
    if source_w <= 0 or source_h <= 0:
        return CanvasSpec(*LANDSCAPE_SIZE, "16:9")
    if _use_native_source_size():
        w, h = _even_px(source_w), _even_px(source_h)
        return CanvasSpec(w, h, _label_for(w, h))
    if source_h > source_w * 1.05:
        return CanvasSpec(*PORTRAIT_SIZE, "9:16")
    return CanvasSpec(*LANDSCAPE_SIZE, "16:9")


def output_aspect_mode() -> str:
    return os.getenv("HONGGUO_OUTPUT_ASPECT", "auto").strip().lower()


def canvas_from_env() -> Optional[CanvasSpec]:
    """显式 HONGGUO_OUTPUT_ASPECT 或同时指定 OUTPUT_WIDTH/HEIGHT。"""
    mode = output_aspect_mode()
    if mode in ("16:9", "landscape", "横屏", "horizontal"):
        return CanvasSpec(*LANDSCAPE_SIZE, "16:9")
    if mode in ("9:16", "portrait", "竖屏", "vertical"):
        return CanvasSpec(*PORTRAIT_SIZE, "9:16")
    w_raw = os.getenv("HONGGUO_OUTPUT_WIDTH", "").strip()
    h_raw = os.getenv("HONGGUO_OUTPUT_HEIGHT", "").strip()
    if w_raw and h_raw and mode not in ("auto", ""):
        try:
            w, h = int(w_raw), int(h_raw)
            if w > 0 and h > 0:
                return CanvasSpec(w, h, _label_for(w, h))
        except ValueError:
            pass
    return None


def resolve_canvas_for_hook(
    series_id: str,
    episode_item_ids: list[str],
    *,
    probe_fn: Callable[[Path], tuple[int, int]],
) -> CanvasSpec:
    forced = canvas_from_env()
    if forced is not None:
        return forced
    if output_aspect_mode() not in ("auto", ""):
        pass
    from fq_koc_material import find_local_material

    for item_id in episode_item_ids:
        path = find_local_material(series_id, item_id)
        if not path:
            continue
        sw, sh = probe_fn(path)
        if sw > 0 and sh > 0:
            spec = canvas_from_source_size(sw, sh)
            logger.info(
                "源素材 %s %dx%d → 成片 %s %dx%d",
                path.name,
                sw,
                sh,
                spec.label,
                spec.width,
                spec.height,
            )
            return spec
    w = int(os.getenv("HONGGUO_OUTPUT_WIDTH", str(LANDSCAPE_SIZE[0])))
    h = int(os.getenv("HONGGUO_OUTPUT_HEIGHT", str(LANDSCAPE_SIZE[1])))
    return CanvasSpec(w, h, _label_for(w, h))


def activate_canvas(spec: CanvasSpec) -> None:
    """写入环境变量，供 MoviePy / FFmpeg 子线程读取。"""
    os.environ["HONGGUO_OUTPUT_WIDTH"] = str(spec.width)
    os.environ["HONGGUO_OUTPUT_HEIGHT"] = str(spec.height)
    try:
        import moviepy_editor as mpy_mod

        mpy_mod.WORK_WIDTH = spec.width
        mpy_mod.WORK_HEIGHT = spec.height
    except ImportError:
        pass


def sync_hook_generator_globals(hook_generator_module) -> CanvasSpec:
    """让 hook_generator 内 PIL/FFmpeg 与 output_canvas 使用同一画布（避免 1920×1920 混拼）。"""
    w, h = output_size()
    label = _label_for(w, h)
    hook_generator_module.WORK_WIDTH = w
    hook_generator_module.WORK_HEIGHT = h
    hook_generator_module.ASPECT_LABEL = label
    hook_generator_module.PANEL_WIDTH = w
    try:
        import moviepy_editor as mpy_mod

        mpy_mod.WORK_WIDTH = w
        mpy_mod.WORK_HEIGHT = h
    except ImportError:
        pass
    return CanvasSpec(w, h, label)


def output_size() -> tuple[int, int]:
    w = int(os.getenv("HONGGUO_OUTPUT_WIDTH", str(LANDSCAPE_SIZE[0])))
    h = int(os.getenv("HONGGUO_OUTPUT_HEIGHT", str(LANDSCAPE_SIZE[1])))
    return w, h


def ui_scale() -> float:
    """相对设计稿的缩放（竖屏以 1080×1920、横屏以 1920×1080 为基准）。"""
    w, h = output_size()
    if h > w:
        ref_w, ref_h = PORTRAIT_SIZE
    else:
        ref_w, ref_h = LANDSCAPE_SIZE
    if ref_w <= 0 or ref_h <= 0:
        return 1.0
    return max(0.35, min(1.5, min(w / ref_w, h / ref_h)))


def scaled_px(base: float) -> int:
    """片头/封面/字幕等元素随成片分辨率同比缩放。"""
    return max(1, int(round(float(base) * ui_scale())))


def maybe_upgrade_canvas_from_source(
    source_w: int,
    source_h: int,
    *,
    probe_fn: Callable[[Path], tuple[int, int]] | None = None,
) -> bool:
    """下载到正片后按源片更新画布（默认横屏占位 → 源片实际尺寸）。"""
    if canvas_from_env() is not None:
        return False
    if not _use_native_source_size() and output_aspect_mode() not in ("auto", ""):
        return False
    spec = canvas_from_source_size(source_w, source_h)
    ow, oh = output_size()
    if spec.width == ow and spec.height == oh:
        return False
    activate_canvas(spec)
    logger.info(
        "按源片更新成片画幅 %dx%d → %s %dx%d",
        source_w,
        source_h,
        spec.label,
        spec.width,
        spec.height,
    )
    return True


def warn_if_output_aspect_mismatched_source(
    output_path: Path,
    series_id: str,
    episode_item_ids: list[str],
    *,
    probe_fn: Callable[[Path], tuple[int, int]],
) -> None:
    """源片竖屏却输出横屏时，正片只占中间窄条，观感像「分辨率很小」。"""
    out_w, out_h = probe_fn(output_path)
    if out_w <= 0 or out_h <= 0:
        return
    from fq_koc_material import find_local_material

    for item_id in episode_item_ids:
        path = find_local_material(series_id, item_id)
        if not path:
            continue
        sw, sh = probe_fn(path)
        if sw <= 0 or sh <= 0:
            continue
        src_portrait = sh > sw * 1.05
        out_portrait = out_h > out_w
        if src_portrait != out_portrait or (
            abs(out_w - sw) > 8 or abs(out_h - sh) > 8
        ):
            expect = canvas_from_source_size(sw, sh)
            logger.warning(
                "成片 %dx%d 与源片 %s %dx%d 不一致（期望约 %dx%d %s），"
                "可能混用了横竖屏片段；请重启服务后重生成。",
                out_w,
                out_h,
                path.name,
                sw,
                sh,
                expect.width,
                expect.height,
                expect.label,
            )
        elif not src_portrait and out_portrait:
            logger.warning(
                "成片为竖屏 %dx%d，源片 %s 为横屏 %dx%d，上下会有大黑边。",
                out_w,
                out_h,
                path.name,
                sw,
                sh,
            )
        return


def probe_video_size_ffmpeg(path: Path, *, ffmpeg: str = "ffmpeg") -> tuple[int, int]:
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return 0, 0
