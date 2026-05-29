"""手動多段剪輯：草稿方案（空模板或預填兩段高光）。"""

from __future__ import annotations

import logging
from typing import Optional

from ai_edit_planner import BodySegmentPlan, HookEditPlan, plan_to_dict, plan_from_manual_dict
logger = logging.getLogger(__name__)


def scale_manual_clips_to_target(clips: list, target_sec: float) -> None:
    """
    手動時間軸：按正片總預算縮放兩段高光（不受 HONGGUO_CLIP_MAX_SEC=4 的 AI 快切上限）。
  30s 成片時兩段合計應約 24–27s（另加片尾口播等）。
    """
    if not clips or target_sec <= 0:
        return
    n = len(clips)
    lo = max(4.0, target_sec / n * 0.5)
    hi = max(lo + 1.0, min(26.0, target_sec / n * 1.2))
    total = sum(float(c.duration_sec or 0) for c in clips)
    if total < 0.5:
        per = target_sec / n
        for c in clips:
            c.duration_sec = round(_clamp(per, lo, hi), 2)
    else:
        ratio = target_sec / total
        for c in clips:
            c.duration_sec = round(
                _clamp(float(c.duration_sec or 0) * ratio, lo, hi), 2
            )
        drift = target_sec - sum(float(c.duration_sec or 0) for c in clips)
        if abs(drift) > 0.12 and clips:
            last = clips[-1]
            last.duration_sec = round(
                _clamp(float(last.duration_sec or 0) + drift, lo, hi), 2
            )


def apply_manual_hook_clip_budget(
    segments: list[BodySegmentPlan],
    *,
    episode_count: int | None = None,
) -> float:
    """
    手動時間軸預填/成片：把 clips 總時長對齊鉤子正片預算（30s 預設下約扣除片頭片尾）。
    入點仍來自高光分析；只調整每段時長，不是隨機剪。
    """
    from hook_duration_budget import (
        body_budget_seconds,
        hook_budget_enabled,
        hook_target_total_sec,
        intro_outro_overhead_sec,
        scale_body_segments_to_budget,
    )
    from multi_clip import sync_segment_from_clips

    ep_n = max(1, episode_count or len(segments) or 1)
    if not segments or not hook_budget_enabled(ep_n):
        return sum(
            sum(c.duration_sec for c in (s.clips or [])) for s in segments
        )

    budget_total = body_budget_seconds(ep_n)
    if ep_n > 1:
        scale_body_segments_to_budget(segments, episode_count=ep_n)

    for seg in segments:
        if not seg.clips:
            continue
        per_target = budget_total if ep_n == 1 else max(6.0, float(seg.duration_sec or 0))
        scale_manual_clips_to_target(seg.clips, per_target)
        trim, total, synced = sync_segment_from_clips(
            trim_start_sec=0.0,
            duration_sec=sum(c.duration_sec for c in seg.clips),
            clips=seg.clips,
        )
        seg.clips = synced
        seg.trim_start_sec = trim
        seg.duration_sec = total

    body_total = sum(sum(c.duration_sec for c in (s.clips or [])) for s in segments)
    hook_total = hook_target_total_sec()
    overhead = intro_outro_overhead_sec()
    logger.info(
        "手動方案已對齊鉤子預算：正片 %.1fs / %.1fs，預估成片 %.0fs（片头片尾约 %.1fs，%d 集）",
        body_total,
        budget_total,
        hook_total,
        overhead,
        ep_n,
    )
    return body_total


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def gather_highlight_profiles_for_manual(
    *,
    series_id: str,
    episode_item_ids: list[str],
    episode_labels: list[str],
    work_dir,
) -> dict[int, list]:
    """
    手動時間軸預填：強制分析本地正片的音畫高能軸（不受 HONGGUO_VISUAL_PROFILE=0 影響）。
    """
    from pathlib import Path

    from fq_koc_material import find_local_material, has_usable_local_episode
    from video_moment_profile import build_visual_profile

    out: dict[int, list] = {}
    base_work = Path(work_dir) if work_dir else Path(".")
    base_work.mkdir(parents=True, exist_ok=True)
    for idx, item_id in enumerate(episode_item_ids, start=1):
        label = episode_labels[idx - 1] if idx - 1 < len(episode_labels) else f"第{idx}集"
        if not has_usable_local_episode(series_id, item_id):
            logger.warning("%s 无本地正片，无法分析高光，将使用占位高光位", label)
            continue
        path = find_local_material(series_id, item_id)
        if not path:
            continue
        ep_work = base_work / f"manual_highlight_ep{idx:02d}"
        try:
            moments = build_visual_profile(
                path, ep_work, book_id=series_id, item_id=item_id
            )
        except Exception as exc:
            logger.warning("%s 高光分析失败: %s", label, exc)
            continue
        if moments:
            out[idx] = moments
            logger.info("%s 音画高光轴 %d 段（score Top 用于预填）", label, len(moments))
    return out


def build_manual_draft_dict(
    plan: HookEditPlan,
    *,
    episode_durations: list[float],
    episode_item_ids: Optional[list[str]] = None,
) -> dict:
    """附帶每集源片時長與 item_id，方便前端時間軸載入視頻。"""
    from hook_duration_budget import (
        body_budget_seconds,
        hook_duration_range_text,
        hook_target_total_sec,
        intro_outro_overhead_sec,
    )

    ep_n = max(1, len(episode_item_ids or []))
    d = plan_to_dict(plan)
    d["target_body_sec"] = body_budget_seconds(ep_n)
    d["target_hook_total_sec"] = hook_target_total_sec()
    d["hook_overhead_sec"] = intro_outro_overhead_sec()
    d["hook_range_text"] = hook_duration_range_text()
    d["episode_durations"] = list(episode_durations)
    ids = list(episode_item_ids or [])
    for i, seg in enumerate(d.get("body_segments") or []):
        if isinstance(seg, dict) and i < len(ids):
            seg["item_id"] = ids[i]
    return d


async def build_manual_draft_after_materials(
    *,
    series_id: str,
    drama_title: str,
    opening: str,
    keyword: str,
    episode_item_ids: list[str],
    episode_titles: dict[str, str],
) -> dict:
    """生成任务中断后：本地已有正片，据此生成可编辑草稿。"""
    from fq_koc_material import local_material_duration

    labels: list[str] = []
    durations: list[float] = []
    for index, item_id in enumerate(episode_item_ids, start=1):
        labels.append(episode_titles.get(item_id) or f"第{index}集")
        dur = local_material_duration(series_id, item_id)
        durations.append(dur if dur > 1 else 120.0)
    return await build_manual_draft_with_prefill(
        series_id=series_id,
        drama_title=drama_title,
        opening=opening,
        keyword=keyword,
        episode_labels=labels,
        episode_durations=durations,
        episode_item_ids=episode_item_ids,
        prefill="simple",
    )


async def build_manual_draft_with_prefill(
    *,
    series_id: str,
    drama_title: str,
    opening: str,
    keyword: str,
    episode_labels: list[str],
    episode_durations: list[float],
    episode_item_ids: list[str],
    prefill: str,
    work_dir=None,
) -> dict:
    """prefill=simple：分析音画高能轴后预填两段高光（非随机片段）。"""
    import asyncio
    import tempfile
    from pathlib import Path

    from simple_highlight_plan import plan_simple_two_highlight

    mode = (prefill or "simple").strip().lower()
    if mode not in ("simple", "highlight", ""):
        mode = "simple"

    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="manual_highlight_"))
    profiles: dict = {}
    if series_id and episode_item_ids:
        profiles = await asyncio.to_thread(
            gather_highlight_profiles_for_manual,
            series_id=series_id,
            episode_item_ids=episode_item_ids,
            episode_labels=episode_labels,
            work_dir=work,
        )

    analyzed_eps = len(profiles)
    prefill_note = (
        f"已按音画高光预填（{analyzed_eps}/{len(episode_item_ids)} 集已分析）"
        if analyzed_eps
        else "未分析到音画轴（请确认正片已缓存到本地）"
    )

    try:
        plan = plan_simple_two_highlight(
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
            episode_visual_profiles=profiles or None,
        )
        raw = plan_to_dict(plan)
        raw["hook_summary"] = f"手動多段剪輯（{prefill_note}，可拖拽修改）"
        plan = plan_from_manual_dict(
            raw,
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
        )
        draft = build_manual_draft_dict(
            plan,
            episode_durations=episode_durations,
            episode_item_ids=episode_item_ids,
        )
        draft["prefill_source"] = "highlight" if analyzed_eps else "fallback"
        return draft
    except Exception as exc:
        logger.exception("手動草稿高光預填失敗: %s", exc)
        raise RuntimeError(f"载入高光草稿失败: {exc}") from exc
