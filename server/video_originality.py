"""抖音等平台判重：正片二创变换（视觉微调 + 解说字幕 + 元数据清理）。"""

from __future__ import annotations

import hashlib
import os
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def originality_enabled() -> bool:
    v = os.getenv("HONGGUO_ORIGINALITY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def originality_outro_enabled() -> bool:
    v = os.getenv("HONGGUO_ORIGINALITY_OUTRO", "1").strip().lower()
    return originality_enabled() and v not in ("0", "false", "no", "off")


def _seed_int(seed_str: str) -> int:
    digest = hashlib.md5(seed_str.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def trim_jitter_seconds(seed_str: str) -> float:
    rng = random.Random(_seed_int(seed_str))
    return rng.uniform(-2.0, 5.0)


def _clean_line(text: str) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    return line[:36]


def _load_overlay_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_subtitle_overlay_png(
    path: Path,
    text: str,
    *,
    width: int = 1920,
    height: int = 1080,
) -> None:
    """透明底 + 底部解说条，供 ffmpeg overlay 烧录。"""
    line = _clean_line(text)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bar_h = 120
    draw.rectangle(
        (40, height - bar_h - 36, width - 40, height - 36),
        fill=(0, 0, 0, 170),
    )
    font = _load_overlay_font(46)
    box = draw.textbbox((0, 0), line, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    tx = (width - tw) // 2 - box[0]
    ty = height - bar_h // 2 - th // 2 - 36 - box[1]
    draw.text((tx, ty), line, fill=(255, 255, 255, 255), font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def build_visual_filters(seed_str: str, *, width: int, height: int) -> list[str]:
    """微缩放 + 调色 + 锐化 + 顶部进度条，改变帧特征且观感自然。"""
    rng = random.Random(_seed_int(seed_str))
    zoom = 1.02 + rng.uniform(0, 0.03)
    x_j = int(rng.uniform(-18, 18))
    y_j = int(rng.uniform(-12, 12))
    bright = rng.uniform(-0.02, 0.04)
    contrast = rng.uniform(1.02, 1.07)
    sat = rng.uniform(1.02, 1.10)
    hue = rng.uniform(-3, 3)

    return [
        f"scale=ceil(iw*{zoom:.4f}/2)*2:ceil(ih*{zoom:.4f}/2)*2",
        f"crop={width}:{height}:{x_j}+(iw-{width})/2:{y_j}+(ih-{height})/2",
        f"eq=brightness={bright:.3f}:contrast={contrast:.3f}:saturation={sat:.3f}",
        f"hue=h={hue:.1f}",
        "unsharp=3:3:0.35:3:3:0.0",
        f"drawbox=x=0:y=8:w=min(iw\\,iw*t/30):h=5:color=0xFF4D4F@0.85:t=fill",
    ]


def build_body_video_filters(
    *,
    base_fit_filter: str,
    speed: float,
    seed_str: str,
    width: int,
    height: int,
    fps: int,
) -> str:
    parts: list[str] = [base_fit_filter]
    if originality_enabled():
        parts.extend(build_visual_filters(seed_str, width=width, height=height))
    if abs(speed - 1.0) >= 0.01:
        parts.append(f"setpts=PTS/{speed}")
    parts.append(f"fps={fps}")
    parts.append("format=yuv420p")
    return ",".join(parts)


def build_subtitle_overlay_filter_complex(
    *,
    base_vf: str,
    overlay_count: int,
    duration_sec: float,
    lines_count: int,
) -> tuple[str, str]:
    """PNG overlay 烧录解说字幕，返回 (filter_complex, 输出标签)。"""
    dur = max(1.0, float(duration_sec))
    slot = dur / max(1, lines_count)
    chain = f"[0:v]{base_vf}[vbase]"
    prev = "vbase"
    for i in range(overlay_count):
        start = i * slot
        end = min(dur, (i + 1) * slot + 0.02)
        inp = i + 1
        out = f"vsub{i}"
        chain += (
            f";[{prev}][{inp}:v]overlay=0:0:enable='between(t,{start:.2f},{end:.2f})'[{out}]"
        )
        prev = out
    return chain, prev


def pick_commentary_lines(
    *,
    plan_lines: list[str],
    opening_text: str,
    subtitle_hint: str,
    hook_summary: str,
    drama_title: str,
) -> list[str]:
    """合并 AI 解说句，去重并保证至少 2 条。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        for piece in re.split(r"[\n；;。！？!?]+", raw or ""):
            line = _clean_line(piece)
            if line and line not in seen:
                seen.add(line)
                out.append(line)

    for line in plan_lines:
        add(line)
    add(subtitle_hint)
    add(opening_text)
    add(hook_summary)

    short = (drama_title or "短剧").strip()[:10]
    fallbacks = [
        f"《{short}》这段太炸了",
        "注意看男主这个眼神",
        "反转来得猝不及防",
        f"红果搜「{short}」看全集",
    ]
    for fb in fallbacks:
        if len(out) >= 4:
            break
        add(fb)
    return out[:5]


def ffmpeg_metadata_strip_args() -> list[str]:
    if not originality_enabled():
        return []
    return ["-map_metadata", "-1", "-metadata", "title=", "-metadata", "comment="]
