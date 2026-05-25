"""Meme 风二创：AI 时间轴字幕 + 卡点缩放/抖动（MoviePy）。"""

from __future__ import annotations

import hashlib
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont


def meme_edit_enabled() -> bool:
    v = os.getenv("HONGGUO_MEME_EDIT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def meme_on_body_enabled() -> bool:
    """正片是否叠 meme 大字幕/卡点（原味模式默认关，片头片尾仍可走 AI 文案）。"""
    if not meme_edit_enabled():
        return False
    v = os.getenv("HONGGUO_MEME_ON_BODY", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    try:
        from video_originality import authentic_preservation_enabled

        return not authentic_preservation_enabled()
    except ImportError:
        return True


@dataclass
class MemeCaption:
    text: str
    at_sec: float = 0.0
    duration_sec: float = 1.4
    position: str = "top"  # top | center | impact | bottom
    style: str = "punch"  # punch | shock | whisper


@dataclass
class MemeBeat:
    at_sec: float = 0.0
    effect: str = "zoom_punch"  # zoom_punch | shake
    intensity: float = 1.12
    duration_sec: float = 0.35


def _seed_int(seed_str: str) -> int:
    return int(hashlib.md5(seed_str.encode("utf-8")).hexdigest()[:8], 16)


def _clean_meme_text(text: str, *, max_len: int = 22) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    if len(line) > max_len:
        line = line[: max_len - 1] + "…"
    return line


def _load_meme_font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def render_meme_caption_png(
    path: Path,
    text: str,
    *,
    width: int = 1920,
    height: int = 1080,
    position: str = "top",
    style: str = "punch",
) -> None:
    """透明底大字幕（白字黑描 / 冲击黄字）。"""
    line = _clean_meme_text(text)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pos = (position or "top").lower()
    sty = (style or "punch").lower()
    if pos == "impact" or sty == "shock":
        font_size = 96
        fill = (255, 230, 40, 255) if sty == "shock" else (255, 255, 255, 255)
        stroke = 6
    elif sty == "whisper":
        font_size = 52
        fill = (220, 220, 220, 230)
        stroke = 3
    else:
        font_size = 72
        fill = (255, 255, 255, 255)
        stroke = 5

    font = _load_meme_font(font_size)
    box = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
    tw, th = box[2] - box[0], box[3] - box[1]
    tx = (width - tw) // 2 - box[0]
    if pos in ("center", "impact"):
        ty = (height - th) // 2 - box[1]
    elif pos == "bottom":
        ty = height - th - 140 - box[1]
    else:
        ty = 72 - box[1]

    if sty == "shock":
        pad = 24
        draw.rectangle(
            (tx - pad, ty - pad, tx + tw + pad, ty + th + pad),
            fill=(0, 0, 0, 200),
        )

    draw.text(
        (tx, ty),
        line,
        font=font,
        fill=fill,
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 255),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def parse_meme_caption(item: Any) -> Optional[MemeCaption]:
    if isinstance(item, str):
        t = _clean_meme_text(item)
        if not t:
            return None
        return MemeCaption(text=t)
    if not isinstance(item, dict):
        return None
    text = _clean_meme_text(str(item.get("text") or item.get("caption") or ""))
    if not text:
        return None
    return MemeCaption(
        text=text,
        at_sec=max(0.0, float(item.get("at_sec") or item.get("start_sec") or 0)),
        duration_sec=_clamp(
            float(item.get("duration_sec") or item.get("duration") or 1.4),
            0.6,
            4.0,
        ),
        position=str(item.get("position") or "top"),
        style=str(item.get("style") or "punch"),
    )


def parse_meme_beat(item: Any) -> Optional[MemeBeat]:
    if not isinstance(item, dict):
        return None
    effect = str(item.get("effect") or "zoom_punch").lower()
    return MemeBeat(
        at_sec=max(0.0, float(item.get("at_sec") or item.get("start_sec") or 0)),
        effect=effect if effect in ("zoom_punch", "shake") else "zoom_punch",
        intensity=_clamp(float(item.get("intensity") or 1.12), 1.05, 1.28),
        duration_sec=_clamp(
            float(item.get("duration_sec") or item.get("duration") or 0.35),
            0.15,
            0.8,
        ),
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalize_meme_captions(raw: Any) -> list[MemeCaption]:
    if not isinstance(raw, list):
        return []
    out: list[MemeCaption] = []
    for item in raw:
        cap = parse_meme_caption(item)
        if cap:
            out.append(cap)
    return out[:3]


def normalize_meme_beats(raw: Any) -> list[MemeBeat]:
    if not isinstance(raw, list):
        return []
    out: list[MemeBeat] = []
    for item in raw:
        beat = parse_meme_beat(item)
        if beat:
            out.append(beat)
    return out[:2]


def default_meme_captions(drama_title: str, episode_index: int = 1) -> list[MemeCaption]:
    from platform_compliance import douyin_safe_enabled

    short = (drama_title or "短剧").strip()[:8]
    bottom = "关注看全集" if douyin_safe_enabled() else f"红果搜{short}"
    templates = [
        ("前方高能！", 1.2, "top", "punch"),
        ("这反转绝了", 8.0, "impact", "shock"),
        (bottom, 16.0, "bottom", "whisper"),
    ]
    return [
        MemeCaption(
            text=t,
            at_sec=a + (episode_index - 1) * 0.3,
            duration_sec=2.2,
            position=p,
            style=s,
        )
        for t, a, p, s in templates
    ]


def default_meme_beats(duration: float) -> list[MemeBeat]:
    if duration < 4:
        return []
    anchors = [duration * 0.32, duration * 0.68]
    return [
        MemeBeat(
            at_sec=_clamp(a, 0.5, max(0.5, duration - 0.5)),
            effect="zoom_punch",
            intensity=1.16,
            duration_sec=0.32,
        )
        for a in anchors
    ]


def distribute_captions_on_timeline(
    captions: list[MemeCaption],
    duration: float,
) -> list[MemeCaption]:
    """无 at_sec 的条目按槽位均分。"""
    if duration < 0.5 or not captions:
        return []
    need_slot = [c for c in captions if c.at_sec <= 0.01]
    fixed = [c for c in captions if c.at_sec > 0.01]
    if need_slot:
        slot = duration / (len(need_slot) + 1)
        for i, cap in enumerate(need_slot):
            cap.at_sec = slot * (i + 1)
    out = fixed + need_slot
    out.sort(key=lambda c: c.at_sec)
    cleaned: list[MemeCaption] = []
    for cap in out:
        cap.at_sec = _clamp(cap.at_sec, 0.0, max(0.0, duration - 0.5))
        cap.duration_sec = min(cap.duration_sec, max(0.6, duration - cap.at_sec))
        cleaned.append(cap)
    return cleaned[:3]


def finalize_meme_captions_for_clip(
    captions: list[MemeCaption],
    duration: float,
    *,
    max_count: int = 3,
) -> list[MemeCaption]:
    """去重叠、拉时长、轮换位置，避免字幕糊成一团。"""
    caps = distribute_captions_on_timeline(captions, duration)[:max_count]
    if not caps:
        return []
    caps.sort(key=lambda c: c.at_sec)
    slot_positions = ("top", "impact", "bottom")
    for i, cap in enumerate(caps):
        cap.duration_sec = max(2.0, min(cap.duration_sec, 2.8))
        if cap.position == "center":
            cap.position = slot_positions[i % len(slot_positions)]
        elif i == 1 and cap.position == "top":
            cap.position = "impact"
    min_gap = 0.5
    for i in range(1, len(caps)):
        prev_end = caps[i - 1].at_sec + caps[i - 1].duration_sec
        if caps[i].at_sec < prev_end + min_gap:
            caps[i].at_sec = prev_end + min_gap
        caps[i].duration_sec = min(
            caps[i].duration_sec,
            max(1.2, duration - caps[i].at_sec - 0.2),
        )
    return [c for c in caps if c.at_sec < duration - 0.8]


def apply_meme_effects(
    clip,
    *,
    captions: list[MemeCaption],
    beats: list[MemeBeat],
    work_dir: Path,
    prefix: str,
    seed_str: str = "",
) -> object:
    """对正片 clip 施加 meme 字幕与卡点特效。"""
    from moviepy import CompositeVideoClip, ImageClip, concatenate_videoclips

    dur = float(clip.duration or 0)
    if dur < 0.3:
        return clip

    out = clip
    beat_list = sorted(beats, key=lambda b: b.at_sec, reverse=True)
    for beat in beat_list:
        if beat.at_sec >= dur - 0.05:
            continue
        if beat.effect == "shake":
            shake_beat = MemeBeat(
                at_sec=beat.at_sec,
                effect="zoom_punch",
                intensity=_clamp(beat.intensity, 1.08, 1.18),
                duration_sec=min(0.28, beat.duration_sec),
            )
            out = _apply_zoom_punch(out, shake_beat)
        else:
            out = _apply_zoom_punch(out, beat)

    caps = finalize_meme_captions_for_clip(
        captions, float(out.duration or dur), max_count=3
    )
    if not caps:
        return out

    layers = [out]
    for i, cap in enumerate(caps):
        png = work_dir / f"{prefix}_meme_{i}.png"
        render_meme_caption_png(
            png,
            cap.text,
            width=out.size[0],
            height=out.size[1],
            position=cap.position,
            style=cap.style,
        )
        layers.append(
            ImageClip(str(png))
            .with_duration(cap.duration_sec)
            .with_start(cap.at_sec)
            .with_position((0, 0))
        )
    return CompositeVideoClip(layers, size=out.size).with_duration(out.duration)


def _apply_zoom_punch(clip, beat: MemeBeat):
    from moviepy import concatenate_videoclips

    dur = float(clip.duration or 0)
    half = beat.duration_sec / 2
    t0 = max(0.0, beat.at_sec - half)
    t1 = min(dur, beat.at_sec + half)
    if t1 - t0 < 0.08:
        return clip

    parts = []
    if t0 > 0.02:
        parts.append(clip.subclipped(0, t0))
    mid = clip.subclipped(t0, t1)
    cw, ch = mid.size
    zoomed = mid.resized(beat.intensity)
    nw, nh = zoomed.size
    x1 = max(0, (nw - cw) // 2)
    y1 = max(0, (nh - ch) // 2)
    parts.append(zoomed.cropped(x1=x1, y1=y1, x2=x1 + cw, y2=y1 + ch))
    if t1 < dur - 0.02:
        parts.append(clip.subclipped(t1, dur))
    if len(parts) == 1:
        return parts[0]
    return concatenate_videoclips(parts, method="compose")

