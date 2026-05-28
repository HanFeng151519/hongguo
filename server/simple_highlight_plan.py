"""无 AI：每集取 score 最高的两段高光，正片时长对齐 30s 钩子预算。"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ai_edit_planner import (
    BodySegmentPlan,
    HookEditPlan,
    _title_short,
    apply_fixed_opening_to_plan,
)
from edge_tts_narration import fixed_opening_text
from hook_duration_budget import (
    body_budget_seconds,
    hook_duration_range_text,
    scale_body_segments_to_budget,
)
from multi_clip import ClipFragment, sync_segment_from_clips
from platform_compliance import safe_post_caption
from video_moment_profile import VisualMoment

logger = logging.getLogger(__name__)


def simple_highlight_enabled() -> bool:
    """默认走两段高光直剪（不调用 LLM）。"""
    v = os.getenv("HONGGUO_SIMPLE_HIGHLIGHT_EDIT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def clips_per_episode_simple() -> int:
    raw = os.getenv("HONGGUO_SIMPLE_HIGHLIGHT_CLIPS", "2").strip()
    try:
        return max(1, min(4, int(raw)))
    except ValueError:
        return 2


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _moment_mid(m: VisualMoment) -> float:
    return (m.start_sec + m.end_sec) * 0.5


def _pick_top_moments(
    moments: list[VisualMoment],
    n: int,
    *,
    min_gap_sec: float = 10.0,
) -> list[VisualMoment]:
    if not moments or n < 1:
        return []
    ranked = sorted(moments, key=lambda m: (-m.score, m.start_sec))
    picked: list[VisualMoment] = []
    for m in ranked:
        mid = _moment_mid(m)
        if any(abs(mid - _moment_mid(p)) < min_gap_sec for p in picked):
            continue
        picked.append(m)
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda m: m.start_sec)


def _fallback_moments(
    dur_avail: float,
    n: int,
    *,
    intro_skip: float = 0.0,
) -> list[VisualMoment]:
    """无画面轴时：跳过片头后均分取点。"""
    avail = max(20.0, float(dur_avail) - intro_skip - 5.0)
    base = intro_skip + 3.0
    fracs = [0.22, 0.52, 0.78, 0.9][:n]
    out: list[VisualMoment] = []
    for i, frac in enumerate(fracs):
        mid = base + avail * frac
        span = min(12.0, avail * 0.18)
        out.append(
            VisualMoment(
                max(intro_skip, mid - span * 0.4),
                min(dur_avail - 0.5, mid + span * 0.6),
                tags="fallback",
                score=0.35 - i * 0.03,
                note="默认高光位（未检测到音画峰）",
            )
        )
    return out


def _moments_to_clips(
    moments: list[VisualMoment],
    *,
    dur_avail: float,
    target_sec: float,
    intro_skip: float = 0.0,
) -> list[ClipFragment]:
    n = max(1, len(moments))
    target = max(6.0, float(target_sec))
    per = target / n
    lo = max(5.0, per * 0.72)
    hi = min(max(per * 1.28, lo + 1.0), dur_avail * 0.42, 20.0)
    avail = max(lo + 1.0, float(dur_avail) - 0.5)

    clips: list[ClipFragment] = []
    for m in moments:
        span = max(m.end_sec - m.start_sec, lo)
        dur = _clamp(span, lo, hi)
        start = _clamp(m.start_sec - 0.25, intro_skip, avail - dur)
        if start + dur > avail:
            dur = max(lo, avail - start)
        reason = "A|本集高光"
        if "fight" in m.tags or "motion" in m.tags:
            reason = "A|高能画面"
        elif "sfx_high" in m.tags:
            reason = "A|音效冲击"
        if m.note:
            reason = f"{reason}：{m.note[:20]}"
        clips.append(
            ClipFragment(
                trim_start_sec=round(start, 2),
                duration_sec=round(dur, 2),
                reason=reason,
            )
        )

    total = sum(c.duration_sec for c in clips)
    if clips and abs(total - target) > 0.6:
        ratio = target / max(total, 0.01)
        for c in clips:
            c.duration_sec = round(_clamp(c.duration_sec * ratio, lo, hi), 2)
        drift = target - sum(c.duration_sec for c in clips)
        if abs(drift) > 0.25 and clips:
            clips[-1].duration_sec = round(
                _clamp(clips[-1].duration_sec + drift, lo, hi), 2
            )
    return clips


def _merge_nearby_clips(
    clips: list[ClipFragment], *, max_gap_sec: float = 1.0
) -> list[ClipFragment]:
    """
    两段高光若重叠或间隔过小，直接合并为一段，避免观感重复。
    max_gap_sec=1.0 表示前后相距 <=1s 也视为同一段。
    """
    if len(clips) < 2:
        return clips
    ordered = sorted(clips, key=lambda c: c.trim_start_sec)
    merged: list[ClipFragment] = [ordered[0]]
    for cur in ordered[1:]:
        prev = merged[-1]
        prev_end = prev.trim_start_sec + prev.duration_sec
        gap = cur.trim_start_sec - prev_end
        if gap <= max_gap_sec:
            new_start = min(prev.trim_start_sec, cur.trim_start_sec)
            new_end = max(prev_end, cur.trim_start_sec + cur.duration_sec)
            merged[-1] = ClipFragment(
                trim_start_sec=round(new_start, 2),
                duration_sec=round(max(0.8, new_end - new_start), 2),
                reason="A|高光合并：相邻片段去重",
            )
            continue
        merged.append(cur)
    return merged


def plan_simple_two_highlight(
    *,
    drama_title: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
    episode_visual_profiles: Optional[dict[int, list[VisualMoment]]] = None,
) -> HookEditPlan:
    short = _title_short(drama_title)
    ep_n = len(episode_labels) or 1
    n_clips = clips_per_episode_simple()
    budget = body_budget_seconds(ep_n)
    try:
        from hook_duration_budget import hook_target_total_sec
        from hook_timeline import intro_outro_overhead_pro

        hook_body = hook_target_total_sec() - intro_outro_overhead_pro()
        if hook_body > budget + 0.5:
            budget = hook_body
    except ImportError:
        pass
    per_ep = budget / ep_n
    profiles = episode_visual_profiles or {}

    segments: list[BodySegmentPlan] = []
    for i, (label, dur_f) in enumerate(zip(episode_labels, episode_durations)):
        dur_avail = max(30.0, float(dur_f or 120.0))
        intro_skip = 0.0
        mat_hint = profiles.get(i + 1)
        moments = list(mat_hint or [])
        if not moments:
            moments = _fallback_moments(dur_avail, n_clips, intro_skip=intro_skip)
        else:
            moments = _pick_top_moments(moments, n_clips)

        clips = _moments_to_clips(
            moments,
            dur_avail=dur_avail,
            target_sec=per_ep,
            intro_skip=intro_skip,
        )
        before_merge = len(clips)
        clips = _merge_nearby_clips(clips, max_gap_sec=1.0)
        if len(clips) < before_merge:
            logger.info(
                "第%d集 两段高光存在重叠/贴边，已自动合并为 %d 段",
                i + 1,
                len(clips),
            )
        trim, total, clips = sync_segment_from_clips(
            trim_start_sec=clips[0].trim_start_sec if clips else 0.0,
            duration_sec=per_ep,
            clips=clips,
        )
        segments.append(
            BodySegmentPlan(
                episode_index=i + 1,
                trim_start_sec=trim,
                duration_sec=total,
                label=label,
                reason=f"两段高光直剪 {len(clips)} 段",
                clips=clips,
            )
        )
        logger.info(
            "第%d集 两段高光：%d 段 → %.1fs（预算 %.1fs）",
            i + 1,
            len(clips),
            total,
            per_ep,
        )

    if ep_n > 1:
        scale_body_segments_to_budget(segments, episode_count=ep_n)

    opening_text = (
        fixed_opening_text()
        or (opening or "").strip()
        or f"《{short}》高能片段"
    )
    outro_keyword = (keyword or "").strip() or short
    total_body = sum(s.duration_sec for s in segments)
    plan = HookEditPlan(
        opening_text=opening_text,
        opening_seconds=0.0,
        outro_keyword=outro_keyword,
        outro_seconds=0.0,
        body_segments=segments,
        hook_summary=(
            f"两段高光直剪（无 AI）：{ep_n} 集×{n_clips} 段，"
            f"正片 {total_body:.0f}s，成片 {hook_duration_range_text()}"
        ),
        post_caption=safe_post_caption(short),
        commentary_lines=[],
        edit_style="authentic",
    )
    apply_fixed_opening_to_plan(plan)
    return plan
