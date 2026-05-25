"""每集多片段快切：按「冲突→打脸→反转→悬念」弧线取高能片段。"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass
from typing import Any

# (reason 模板, 集内时间比例, 时长权重——反转段略长)
_HOOK_ARC_4 = [
    ("起冲突：挑衅/羞辱/危机爆发", 0.12, 0.92),
    ("高能打脸：碾压/放狠话/爽点", 0.36, 1.05),
    ("剧情反转：身份/误会/真相揭晓", 0.58, 1.18),
    ("悬念断点：下一集必看", 0.82, 1.05),
]
_HOOK_ARC_3 = [
    ("开篇钩子：异象或冲突", 0.10, 0.95),
    ("高能+反转：打脸后反转", 0.48, 1.15),
    ("悬念卡断", 0.82, 1.05),
]
_HOOK_ARC_5 = [
    ("起冲突", 0.10, 0.9),
    ("矛盾升级", 0.26, 0.95),
    ("高能打脸", 0.42, 1.05),
    ("神反转", 0.62, 1.18),
    ("悬念断点", 0.84, 1.05),
]
_HOOKY_KEYWORDS = (
    "反转",
    "打脸",
    "冲突",
    "高能",
    "悬念",
    "爆发",
    "羞辱",
    "碾压",
    "身份",
    "真相",
    "危机",
    "决裂",
    "钩子",
    "BOSS",
    "逆袭",
    "暴露",
    "惊",
    "狠",
)


@dataclass
class ClipFragment:
    trim_start_sec: float
    duration_sec: float
    reason: str = ""


def multi_clip_enabled() -> bool:
    v = os.getenv("HONGGUO_MULTI_CLIP", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def clips_per_episode() -> int:
    raw = os.getenv("HONGGUO_CLIPS_PER_EPISODE", "4").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 4
    return max(2, min(6, n))


def clip_min_sec() -> float:
    return max(4.0, float(os.getenv("HONGGUO_CLIP_MIN_SEC", "7")))


def clip_max_sec() -> float:
    return max(clip_min_sec() + 1.0, float(os.getenv("HONGGUO_CLIP_MAX_SEC", "13")))


def clip_tail_pad_sec() -> float:
    """每段多留尾音，避免台词说到一半被硬切。"""
    return max(0.0, min(2.5, float(os.getenv("HONGGUO_CLIP_TAIL_PAD_SEC", "0.9"))))


def clip_crossfade_sec() -> float:
    """段与段之间交叉淡化，减轻突兀跳切。"""
    return max(0.0, min(0.8, float(os.getenv("HONGGUO_CLIP_CROSSFADE_SEC", "0.28"))))


def clip_min_gap_sec() -> float:
    """源片上相邻片段起点最小间隔，避免重复同一句话。"""
    return max(1.0, float(os.getenv("HONGGUO_CLIP_MIN_GAP_SEC", "3")))


def hook_arc_hint_text() -> str:
    return "冲突起势 → 高能打脸 → 剧情反转 → 悬念断点（每段 reason 须写明类型）"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _reason_is_hooky(reason: str) -> bool:
    text = (reason or "").strip()
    if len(text) < 2:
        return False
    return any(k in text for k in _HOOKY_KEYWORDS)


def _beat_templates(n: int, episode_index: int) -> list[tuple[str, float, float]]:
    if n <= 3:
        beats = list(_HOOK_ARC_3)
    elif n == 4:
        beats = list(_HOOK_ARC_4)
    elif n == 5:
        beats = list(_HOOK_ARC_5)
    else:
        beats = list(_HOOK_ARC_5) + [
            ("余震反应", 0.72, 0.9),
        ]
    beats = beats[:n]
    if episode_index == 1:
        beats[0] = ("开篇钩子：3秒抓眼冲突", beats[0][1] - 0.02, beats[0][2])
    elif episode_index >= 3:
        # 中后段集数：略往后取，避免重复铺垫
        beats = [
            (r, min(0.92, f + 0.04), w) for r, f, w in beats
        ]
    return beats


def _allocate_durations(
    target_sec: float,
    n: int,
    weight_tpl: list[tuple[str, float, float]],
    rng: random.Random,
) -> list[float]:
    min_d = clip_min_sec()
    max_d = clip_max_sec()
    weights = [w * rng.uniform(0.92, 1.08) for _, _, w in weight_tpl]
    wsum = sum(weights) or 1.0
    durs = [_clamp(target_sec * w / wsum, min_d, max_d) for w in weights]
    drift = sum(durs) - target_sec
    if abs(drift) > 0.5 and durs:
        durs[-1] = _clamp(durs[-1] - drift, min_d, max_d)
    return durs


def normalize_clip_fragment(item: Any) -> ClipFragment | None:
    if not isinstance(item, dict):
        return None
    start = float(item.get("trim_start_sec") or item.get("start_sec") or 0)
    dur = float(item.get("duration_sec") or item.get("duration") or 0)
    if dur < 1.0:
        return None
    reason = str(item.get("reason") or item.get("label") or "").strip()
    return ClipFragment(
        trim_start_sec=max(0.0, start),
        duration_sec=dur,
        reason=reason,
    )


def normalize_clip_list(raw: Any) -> list[ClipFragment]:
    if not isinstance(raw, list):
        return []
    out: list[ClipFragment] = []
    for item in raw:
        frag = normalize_clip_fragment(item)
        if frag:
            out.append(frag)
    return _dedupe_and_sort_clips(out)


def _dedupe_and_sort_clips(clips: list[ClipFragment]) -> list[ClipFragment]:
    if not clips:
        return []
    clips = sorted(clips, key=lambda c: c.trim_start_sec)
    merged: list[ClipFragment] = []
    for c in clips:
        if not merged:
            merged.append(c)
            continue
        prev = merged[-1]
        prev_end = prev.trim_start_sec + prev.duration_sec
        if c.trim_start_sec < prev_end - 0.5:
            c = ClipFragment(
                trim_start_sec=prev_end + 0.35,
                duration_sec=c.duration_sec,
                reason=c.reason,
            )
        merged.append(c)
    return merged


def refine_clips_for_hook_arc(
    clips: list[ClipFragment],
    *,
    dur_avail: float,
    target_sec: float,
    rng: random.Random | None = None,
    episode_index: int = 1,
) -> list[ClipFragment]:
    """按高能弧线校正片段起点/时长/reason（保留 AI 给出的高能时间点）。"""
    if not clips:
        return []
    rng = rng or random.Random(episode_index)
    avail = max(30.0, float(dur_avail or 120) - 5.0)
    target = max(clip_min_sec() * len(clips), float(target_sec or sum(c.duration_sec for c in clips)))
    templates = _beat_templates(len(clips), episode_index)
    durs = _allocate_durations(target, len(clips), templates, rng)

    out: list[ClipFragment] = []
    for i, ((reason_tpl, frac, _), dur) in enumerate(zip(templates, durs)):
        orig = clips[i] if i < len(clips) else clips[-1]
        jitter = rng.uniform(-0.035, 0.035)
        anchor = _clamp((avail - dur - 2.0) * (frac + jitter), 0.0, avail - dur - 1.0)
        if _reason_is_hooky(orig.reason) and orig.trim_start_sec > 0:
            start = 0.45 * anchor + 0.55 * orig.trim_start_sec
            reason = orig.reason
        else:
            start = anchor
            reason = reason_tpl
        out.append(
            ClipFragment(
                trim_start_sec=start,
                duration_sec=dur,
                reason=reason,
            )
        )
    return ensure_clip_spacing(_dedupe_and_sort_clips(out))


def ensure_clip_spacing(clips: list[ClipFragment]) -> list[ClipFragment]:
    """相邻片段在源时间轴上拉开间距，并保证最短时长。"""
    if not clips:
        return []
    min_d = clip_min_sec()
    gap = clip_min_gap_sec()
    out: list[ClipFragment] = []
    for i, c in enumerate(clips):
        start = c.trim_start_sec
        if out:
            prev = out[-1]
            prev_end = prev.trim_start_sec + prev.duration_sec
            if start < prev_end + gap:
                start = prev_end + gap
        dur = max(min_d, c.duration_sec)
        out.append(
            ClipFragment(trim_start_sec=start, duration_sec=dur, reason=c.reason)
        )
    return out


def build_default_clips(
    dur_avail: float,
    target_sec: float,
    rng: random.Random,
    *,
    episode_index: int = 1,
) -> list[ClipFragment]:
    """无 AI 时按短剧高能弧线生成快切点。"""
    n = clips_per_episode()
    n = max(2, min(n, int(max(clip_min_sec() * 2, target_sec) / clip_min_sec())))
    templates = _beat_templates(n, episode_index)
    placeholders = [
        ClipFragment(0.0, target_sec / n, reason=t[0]) for t in templates
    ]
    return ensure_clip_spacing(
        refine_clips_for_hook_arc(
            placeholders,
            dur_avail=dur_avail,
            target_sec=target_sec,
            rng=rng,
            episode_index=episode_index,
        )
    )


def sync_segment_from_clips(
    *,
    trim_start_sec: float,
    duration_sec: float,
    clips: list[ClipFragment],
) -> tuple[float, float, list[ClipFragment]]:
    if clips:
        total = sum(c.duration_sec for c in clips)
        return clips[0].trim_start_sec, total, clips
    return trim_start_sec, duration_sec, []


def clip_export_overhead_sec(n_clips: int) -> float:
    """快切导出相对计划时长的净增减：尾音加长 − 段间交叉淡化重叠。"""
    if n_clips <= 0:
        return 0.0
    return n_clips * clip_tail_pad_sec() - max(0, n_clips - 1) * clip_crossfade_sec()


def scale_clips_to_episode_duration(
    clips: list[ClipFragment],
    target_sec: float,
) -> None:
    if not clips or target_sec <= 0:
        return
    min_d = clip_min_sec()
    max_d = clip_max_sec()
    overhead = clip_export_overhead_sec(len(clips))
    target_sec = max(min_d * len(clips), target_sec - overhead)
    total = sum(c.duration_sec for c in clips)
    if total < 0.01:
        per = target_sec / len(clips)
        for c in clips:
            c.duration_sec = _clamp(per, min_d, max_d)
        return
    if abs(total - target_sec) <= 1.0:
        return
    ratio = target_sec / total
    for c in clips:
        c.duration_sec = _clamp(c.duration_sec * ratio, min_d, max_d)
    total2 = sum(c.duration_sec for c in clips)
    if total2 > target_sec + 0.5:
        ratio2 = target_sec / total2
        for c in clips:
            c.duration_sec = _clamp(c.duration_sec * ratio2, min_d, max_d)


def clip_summary_for_log(clips: list[ClipFragment]) -> str:
    return " | ".join(
        f"{c.reason or '片段'}@{c.trim_start_sec:.0f}s/{c.duration_sec:.0f}s"
        for c in clips[:6]
    )
