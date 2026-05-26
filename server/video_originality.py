"""抖音等平台判重：正片二创变换（视觉微调 + 解说字幕 + 元数据清理）。"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def originality_enabled() -> bool:
    v = os.getenv("HONGGUO_ORIGINALITY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def originality_outro_enabled() -> bool:
    if not originality_enabled():
        return False
    v = os.getenv("HONGGUO_ORIGINALITY_OUTRO", "").strip().lower()
    try:
        from platform_compliance import douyin_safe_enabled

        if douyin_safe_enabled():
            # 抖音合规默认关闭「搜 App 看全集」片尾；需片尾时设 HONGGUO_ORIGINALITY_OUTRO=1（站内话术）
            return v in ("1", "true", "yes", "on")
    except ImportError:
        pass
    if not v:
        return True
    return v not in ("0", "false", "no", "off")


def authentic_preservation_enabled() -> bool:
    """尽量保留正片原声原画面；去重主要靠轻度像素特征 + 元数据清理。"""
    v = os.getenv("HONGGUO_AUTHENTIC", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def originality_light_visual() -> bool:
    """轻度去重：微缩放/调色，不画进度条，观感更接近原片。"""
    if not originality_enabled():
        return False
    if not authentic_preservation_enabled():
        return False
    v = os.getenv("HONGGUO_ORIGINALITY_LIGHT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


DEFAULT_BODY_PLAYBACK_SPEED = 1.618


def body_playback_speed() -> float:
    """正片倍速：原味模式固定 1x（保留对白音色）；非原味可读 HONGGUO_BODY_PLAYBACK_SPEED。"""
    raw = os.getenv("HONGGUO_BODY_PLAYBACK_SPEED", "").strip()
    fallback = 1.0 if authentic_preservation_enabled() else DEFAULT_BODY_PLAYBACK_SPEED
    if not raw:
        v = fallback
    else:
        try:
            v = float(raw)
        except ValueError:
            v = fallback
    if v <= 0:
        v = fallback
    v = max(0.5, min(4.0, v))
    if authentic_preservation_enabled() and abs(v - 1.0) > 0.02:
        allow = os.getenv("HONGGUO_AUTHENTIC_ALLOW_SPEED", "").strip().lower()
        if allow not in ("1", "true", "yes", "on"):
            logger.info(
                "原味模式正片强制 1x（忽略 HONGGUO_BODY_PLAYBACK_SPEED=%.3g，"
                "倍速会明显变调）；若确需加速请设 HONGGUO_AUTHENTIC_ALLOW_SPEED=1",
                v,
            )
            return 1.0
    return v


def body_burn_subtitles() -> bool:
    """正片是否烧录底部解说条（原味模式默认关）。"""
    if not originality_enabled():
        return False
    v = os.getenv("HONGGUO_BODY_BURN_SUBTITLES", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return not authentic_preservation_enabled()


def _seed_int(seed_str: str) -> int:
    digest = hashlib.md5(seed_str.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def trim_jitter_seconds(seed_str: str) -> float:
    rng = random.Random(_seed_int(seed_str))
    if authentic_preservation_enabled():
        return rng.uniform(-1.0, 2.5)
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


def build_visual_filters(
    seed_str: str, *, width: int, height: int, light: bool = False
) -> list[str]:
    """微缩放 + 调色 + 锐化；light 模式更贴近原片，仍改变帧哈希过判重。"""
    rng = random.Random(_seed_int(seed_str))
    if light:
        zoom = 1.008 + rng.uniform(0, 0.012)
        x_j = int(rng.uniform(-8, 8))
        y_j = int(rng.uniform(-6, 6))
        bright = rng.uniform(-0.01, 0.02)
        contrast = rng.uniform(1.01, 1.04)
        sat = rng.uniform(1.01, 1.05)
        hue = rng.uniform(-1.5, 1.5)
        filters = [
            f"scale=ceil(iw*{zoom:.4f}/2)*2:ceil(ih*{zoom:.4f}/2)*2",
            f"crop={width}:{height}:{x_j}+(iw-{width})/2:{y_j}+(ih-{height})/2",
            f"eq=brightness={bright:.3f}:contrast={contrast:.3f}:saturation={sat:.3f}",
            f"hue=h={hue:.1f}",
            "unsharp=3:3:0.2:3:3:0.0",
        ]
        return filters

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
        parts.extend(
            build_visual_filters(
                seed_str,
                width=width,
                height=height,
                light=originality_light_visual(),
            )
        )
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
    max_lines: int = 3,
    meme_mode: bool = False,
) -> list[str]:
    """合并 AI 解说句；meme 模式仅保留短句供 TTS，避免与大字幕重复冗长。"""
    try:
        from edge_tts_narration import fixed_opening_text

        if fixed_opening_text():
            return []
    except ImportError:
        pass
    out: list[str] = []
    seen: set[str] = set()
    max_len = 14 if meme_mode else 36

    def add(raw: str) -> None:
        for piece in re.split(r"[\n；;。！？!?]+", raw or ""):
            line = _clean_line(piece)
            if meme_mode and len(line) > max_len:
                line = line[: max_len - 1] + "…"
            if line and line not in seen:
                seen.add(line)
                out.append(line)

    from platform_compliance import safe_commentary_fallbacks, sanitize_promo_copy

    for line in plan_lines:
        add(sanitize_promo_copy(line, max_len=36))
    if not meme_mode:
        add(sanitize_promo_copy(subtitle_hint, max_len=36))
        add(sanitize_promo_copy(opening_text, max_len=36))

    fallbacks = safe_commentary_fallbacks(drama_title, meme_mode=meme_mode)
    for fb in fallbacks:
        if len(out) >= max_lines:
            break
        add(fb)
    return out[:max_lines]


def ffmpeg_metadata_strip_args() -> list[str]:
    if not originality_enabled():
        return []
    return ["-map_metadata", "-1", "-metadata", "title=", "-metadata", "comment="]
