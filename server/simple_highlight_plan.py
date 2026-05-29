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


def _moment_pick_min_gap_sec() -> float:
    """两段高光在源片上的最小间隔（秒），过近易重复画面且易触发平台限流。"""
    raw = os.getenv("HONGGUO_SIMPLE_HIGHLIGHT_MIN_GAP_SEC", "18").strip()
    try:
        return max(8.0, float(raw))
    except ValueError:
        return 18.0


def _effective_moment_gap_sec(dur_avail: float) -> float:
    """源片较短时自动缩小选点间隔，避免第二段被裁没导致成片只有十几秒。"""
    base = _moment_pick_min_gap_sec()
    avail = max(20.0, float(dur_avail))
    if avail < base * 2.4:
        return max(8.0, min(base, avail * 0.28))
    return base


def _clip_timeline_min_sep_sec() -> float:
    """成片时间轴上两段之间至少留出的间隔（秒），禁止合并为一段。"""
    raw = os.getenv("HONGGUO_SIMPLE_HIGHLIGHT_CLIP_SEP_SEC", "2.5").strip()
    try:
        return max(0.8, float(raw))
    except ValueError:
        return 2.5


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _moment_mid(m: VisualMoment) -> float:
    return (m.start_sec + m.end_sec) * 0.5


def _moment_too_close(a: VisualMoment, b: VisualMoment, min_gap_sec: float) -> bool:
    """高光选点去重：中点过近或时间区间贴边/重叠。"""
    if abs(_moment_mid(a) - _moment_mid(b)) < min_gap_sec:
        return True
    if a.start_sec <= b.end_sec and b.start_sec <= a.end_sec:
        return True
    gap = max(b.start_sec - a.end_sec, a.start_sec - b.end_sec)
    return gap < min_gap_sec * 0.4


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
        if any(_moment_too_close(m, p, min_gap_sec) for p in picked):
            continue
        picked.append(m)
        if len(picked) >= n:
            break
    return sorted(picked, key=lambda m: m.start_sec)


def _pick_moments_for_highlights(
    moments: list[VisualMoment],
    n: int,
    *,
    dur_avail: float,
    intro_skip: float = 0.0,
) -> list[VisualMoment]:
    """保证取满 n 段且时间轴拉开，不足时用 fallback 补位。"""
    if n < 1:
        return []
    gap = _effective_moment_gap_sec(dur_avail)
    picked = _pick_top_moments(moments, n, min_gap_sec=gap) if moments else []
    if len(picked) < n and moments:
        picked = _pick_top_moments(moments, n, min_gap_sec=max(8.0, gap * 0.55))
    if len(picked) < n:
        need = n - len(picked)
        extras = _fallback_moments(dur_avail, need + 2, intro_skip=intro_skip)
        for m in extras:
            if any(_moment_too_close(m, p, gap * 0.45) for p in picked):
                continue
            picked.append(m)
            if len(picked) >= n:
                break
    return sorted(picked[:n], key=lambda m: m.start_sec)


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
    hi = min(
        max(per * 1.28, lo + 1.0),
        dur_avail * 0.42,
        max(22.0, target * 0.55),
    )
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


def _overlap_ratio(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    span = max(0.01, min(a_end - a_start, b_end - b_start))
    return overlap / span


def _separate_clips_no_merge(
    clips: list[ClipFragment],
    *,
    dur_avail: float,
    n_required: int,
    intro_skip: float = 0.0,
) -> list[ClipFragment]:
    """
    两段必须分开：重叠/贴边时错开第二段起点，绝不合并成一段。
    """
    if not clips:
        return clips
    avail = max(5.0, float(dur_avail) - 0.5)
    lo = 5.0
    sep = _clip_timeline_min_sep_sec()
    ordered = sorted(clips, key=lambda c: c.trim_start_sec)[: max(1, n_required)]

    if len(ordered) < n_required:
        extras = _fallback_moments(avail, n_required - len(ordered) + 1, intro_skip=intro_skip)
        for m in extras:
            start = _clamp(m.start_sec, intro_skip, avail - lo)
            ordered.append(
                ClipFragment(
                    trim_start_sec=round(start, 2),
                    duration_sec=round(_clamp(m.end_sec - m.start_sec, lo, 12.0), 2),
                    reason="A|补位高光",
                )
            )
            if len(ordered) >= n_required:
                break

    out: list[ClipFragment] = [ordered[0]]
    for cur in ordered[1:n_required]:
        prev = out[-1]
        prev_end = prev.trim_start_sec + prev.duration_sec
        start = cur.trim_start_sec
        dur = cur.duration_sec
        if start < prev_end + sep:
            start = prev_end + sep
        if start + dur > avail:
            dur = max(lo, avail - start)
        reason = cur.reason
        if dur < lo and avail - start >= lo:
            dur = lo
        elif dur < lo:
            logger.warning("第二段高光无足够片长，改用补位")
            extras = _fallback_moments(avail, 1, intro_skip=intro_skip)
            if not extras:
                continue
            m = extras[0]
            start = _clamp(m.start_sec, prev_end + sep, avail - lo)
            dur = lo
            reason = "A|补位高光"
        if "合并" in reason:
            reason = "A|高光去重（分段保留）"
        out.append(
            ClipFragment(
                trim_start_sec=round(start, 2),
                duration_sec=round(dur, 2),
                reason=reason,
            )
        )

    # 源片起点过近：按选点最小间隔拉开（防重复画面/限流）
    if len(out) >= 2:
        min_start_gap = max(sep, _effective_moment_gap_sec(avail) * 0.55)
        a, b = out[0], out[1]
        need_start = a.trim_start_sec + min_start_gap
        if b.trim_start_sec < need_start:
            new_dur = max(lo, min(b.duration_sec, avail - need_start))
            out[1] = ClipFragment(
                trim_start_sec=round(need_start, 2),
                duration_sec=round(new_dur, 2),
                reason="A|高光去重（拉开源片间隔）",
            )
            logger.info(
                "两段高光源片起点过近，第二段已移至 %.1fs（间隔≥%.0fs）",
                out[1].trim_start_sec,
                min_start_gap,
            )

    # 源片时间重叠过高：强制错开第二段
    if len(out) >= 2:
        a, b = out[0], out[1]
        a_end = a.trim_start_sec + a.duration_sec
        b_end = b.trim_start_sec + b.duration_sec
        if _overlap_ratio(a.trim_start_sec, a_end, b.trim_start_sec, b_end) > 0.35:
            new_start = min(avail - lo, a_end + sep)
            new_dur = max(lo, min(b.duration_sec, avail - new_start))
            out[1] = ClipFragment(
                trim_start_sec=round(new_start, 2),
                duration_sec=round(new_dur, 2),
                reason="A|高光去重（错开重叠区）",
            )
            logger.info(
                "两段高光源片重叠，已错开第二段起点至 %.1fs（不合并）",
                out[1].trim_start_sec,
            )

    return out[:n_required]


def _ensure_clips_fill_target(
    clips: list[ClipFragment],
    *,
    target_sec: float,
    dur_avail: float,
    n_required: int,
) -> list[ClipFragment]:
    """保证两段合计接近目标时长（约 30s 正片），避免只剩一段 ~13s。"""
    if not clips:
        return clips
    avail = max(8.0, float(dur_avail) - 0.5)
    target = max(8.0, float(target_sec))
    lo = max(4.5, target / max(1, n_required) * 0.62)

    total = sum(c.duration_sec for c in clips)
    if total < target * 0.88:
        ratio = target / max(total, 0.01)
        for c in clips:
            c.duration_sec = round(
                _clamp(c.duration_sec * ratio, lo, min(22.0, avail * 0.48)), 2
            )
        drift = target - sum(c.duration_sec for c in clips)
        if abs(drift) > 0.2 and clips:
            last = clips[-1]
            last.duration_sec = round(
                _clamp(last.duration_sec + drift, lo, min(22.0, avail - last.trim_start_sec)),
                2,
            )

    if len(clips) < n_required:
        logger.warning(
            "两段高光仅 %d 段（目标 %d 段、合计 %.1fs），源片可能过短",
            len(clips),
            n_required,
            sum(c.duration_sec for c in clips),
        )
    return clips


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
        moments = (
            _pick_moments_for_highlights(
                moments,
                n_clips,
                dur_avail=dur_avail,
                intro_skip=intro_skip,
            )
            if moments
            else _fallback_moments(dur_avail, n_clips, intro_skip=intro_skip)
        )

        clips = _moments_to_clips(
            moments,
            dur_avail=dur_avail,
            target_sec=per_ep,
            intro_skip=intro_skip,
        )
        before = len(clips)
        clips = _separate_clips_no_merge(
            clips,
            dur_avail=dur_avail,
            n_required=n_clips,
            intro_skip=intro_skip,
        )
        if len(clips) != before or len(clips) < n_clips:
            logger.info(
                "第%d集 两段高光去重：保持 %d 段分开（最小间隔 %.0fs）",
                i + 1,
                len(clips),
                _moment_pick_min_gap_sec(),
            )
        clips = _ensure_clips_fill_target(
            clips,
            target_sec=per_ep,
            dur_avail=dur_avail,
            n_required=n_clips,
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
