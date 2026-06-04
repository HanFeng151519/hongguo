"""成片角落水印（丰丰漫剧推荐等）。"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

DEFAULT_WATERMARK_TEXT = "丰丰漫剧推荐"


def _ffmpeg_exe() -> str:
    from ffmpeg_util import resolve_ffmpeg_exe

    return resolve_ffmpeg_exe()


def watermark_enabled() -> bool:
    v = os.getenv("HONGGUO_WATERMARK", "0").strip().lower()
    return v not in ("0", "false", "no", "off")


def watermark_text() -> str:
    return (
        os.getenv("HONGGUO_WATERMARK_TEXT", DEFAULT_WATERMARK_TEXT).strip()
        or DEFAULT_WATERMARK_TEXT
    )


def _find_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    from ffmpeg_util import cjk_font_paths

    for path in cjk_font_paths():
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_watermark_png(
    path: Path,
    *,
    text: str,
    canvas_w: int = 1920,
    canvas_h: int = 1080,
    margin_x: int = 28,
    margin_y: int = 28,
    font_size: int = 30,
) -> None:
    """全画布透明 PNG，仅右下角标签（供 ffmpeg overlay）。"""
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _find_cjk_font(font_size)
    line = (text or DEFAULT_WATERMARK_TEXT).strip()
    box = draw.textbbox((0, 0), line, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad_x, pad_y = 14, 10
    bar_w = tw + pad_x * 2
    bar_h = th + pad_y * 2
    x2 = canvas_w - margin_x
    y2 = canvas_h - margin_y
    x1 = x2 - bar_w
    y1 = y2 - bar_h
    draw.rounded_rectangle(
        (x1, y1, x2, y2),
        radius=8,
        fill=(0, 0, 0, 150),
    )
    tx = x1 + pad_x - box[0]
    ty = y1 + pad_y - box[1]
    draw.text((tx, ty), line, fill=(255, 255, 255, 230), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _burn_via_ffmpeg_overlay(src: Path, dest: Path, overlay_png: Path) -> bool:
    proc = subprocess.run(
        [
            _ffmpeg_exe(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-i",
            str(overlay_png),
            "-filter_complex",
            "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        logger.warning(
            "ffmpeg 水印叠加失败: %s",
            (proc.stderr or proc.stdout or "")[:400],
        )
        return False
    return dest.is_file() and dest.stat().st_size > 10_000


def _burn_via_moviepy(src: Path, dest: Path, overlay_png: Path) -> bool:
    try:
        from moviepy import CompositeVideoClip, ImageClip, VideoFileClip
    except ImportError:
        return False

    dest.unlink(missing_ok=True)
    clip = VideoFileClip(str(src))
    try:
        ov = ImageClip(str(overlay_png)).with_duration(clip.duration)
        out = CompositeVideoClip([clip, ov], size=clip.size)
        out.write_videofile(
            str(dest),
            fps=int(clip.fps or 30),
            codec="libx264",
            audio_codec="aac",
            preset="fast",
            threads=int(os.getenv("HONGGUO_FFMPEG_THREADS", "4")),
            logger=None,
            ffmpeg_params=["-crf", "20", "-movflags", "+faststart"],
        )
        out.close()
    finally:
        clip.close()
    return dest.is_file() and dest.stat().st_size > 10_000


def burn_corner_watermark(
    src: Path,
    dest: Path,
    *,
    text: str | None = None,
    width: int = 1920,
    height: int = 1080,
) -> bool:
    """右下角水印；失败返回 False。"""
    if not watermark_enabled() or not src.is_file():
        return False
    line = (text or watermark_text()).strip()
    if not line:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="hongguo_wm_") as tmp:
        png = Path(tmp) / "wm.png"
        render_watermark_png(png, text=line, canvas_w=width, canvas_h=height)
        if _burn_via_ffmpeg_overlay(src, dest, png):
            logger.info("已烧录角落水印：%s", line)
            return True
        if _burn_via_moviepy(src, dest, png):
            logger.info("已烧录角落水印（MoviePy）：%s", line)
            return True
    return False
