"""大模型生成短剧推广成片剪辑方案，供 ffmpeg 执行。"""

from __future__ import annotations

import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from video_transcript import TranscriptCue

import httpx

from meme_edit import (
    MemeBeat,
    MemeCaption,
    default_meme_beats,
    default_meme_captions,
    meme_edit_enabled,
    meme_on_body_enabled,
    normalize_meme_beats,
    normalize_meme_captions,
)
from hook_duration_budget import (
    body_budget_seconds,
    hook_budget_enabled,
    hook_duration_range_text,
    hook_target_max_sec,
    hook_target_min_sec,
    hook_target_total_sec,
    per_episode_target_sec,
    scale_body_segments_to_budget,
)
from episode_edit_brief import (
    ai_edit_retry_enabled,
    auto_fix_clip_gaps_in_raw,
    briefs_block_for_prompt,
    build_validation_retry_prompt,
    dedupe_narrative_clips,
    early_body_budget_ratio,
    enforce_opening_from_beats,
    enforce_program_visual_opening,
    few_shot_block_for_prompt,
    narrative_cohesion_enabled,
    extend_clips_to_reason_span,
    polish_arc_pacing_clips,
    restore_arc_body_total,
    polish_clips_narrative_coherence,
    max_late_body_sec,
    min_late_body_sec,
    rebalance_clips_front_heavy,
    repair_clip_durations_in_raw,
    coerce_llm_edit_raw,
    sanitize_hook_only_plan_raw,
    validate_ai_edit_raw,
)
from hook_edit_methodology import (
    ai_editor_autonomy_enabled,
    creative_editor_autonomy_block,
    drama_type_hint_for_prompt,
    editing_rules_block,
    hook_narrative_arc_block,
    human_impact_script_brief,
    opening_hook_pattern_block,
)
from multi_clip import (
    ClipFragment,
    build_default_clips,
    clip_max_sec,
    clip_min_sec,
    clip_summary_for_log,
    clip_tail_pad_sec,
    clips_per_episode,
    ai_clip_strict_enabled,
    apply_ai_clips_strict,
    enforce_hook_only_clips,
    cohesive_edit_enabled,
    ensure_clips_count,
    hook_arc_hint_text,
    multi_clip_enabled,
    normalize_clip_list,
    refine_clips_for_hook_arc,
    scale_clips_to_episode_duration,
    sync_segment_from_clips,
)
from platform_compliance import (
    ai_compliance_rule_block,
    douyin_safe_enabled,
    safe_post_caption,
    sanitize_promo_copy,
)
from edge_tts_narration import fixed_opening_text
from video_originality import authentic_preservation_enabled, body_playback_speed
from qwen_client import (
    chat_completion,
    code_model,
    default_model,
    extract_json_object,
    format_api_error,
    is_configured,
)

logger = logging.getLogger(__name__)

# 产品定位：AI 输出对人最友好、最有冲击力的可执行剪辑脚本
MASTER_EDITOR_PERSONA = (
    "你是抖音全品类短剧「每集独立剪辑导演」（都市/甜宠/悬疑/玄幻/虐恋/家庭等均适用）："
    "结合本片对白/画面/音效轴自主构思，产出「纯钩子、高刺激、快切」的 JSON 分镜。"
    "选片优先级：画面动感/情绪张力 > 音效冲击 > 对白金句；禁止套用其他剧/其他集固定模板。"
    "正片剪辑脚本须控制在 23 秒以内，并集中冲突/对峙/反转高光台词。"
)
MASTER_EDITOR_REJECT = (
    "严禁 C 类：重复/长空镜/闲聊拖沓/慢日常/回忆注水/片头片尾/无关配角。"
    "严禁仅因台词好听而选无动感画面；严禁乱序大跳（相邻 clip 间隔>20s 须有 fight/motion 理由）；"
    "严禁 clips 总时长与 body duration_sec 不一致。"
)


def _prompt_char_cap() -> int:
    raw = os.getenv("HONGGUO_LLM_PROMPT_MAX_CHARS", "").strip()
    if not raw:
        return 2600
    try:
        return max(800, min(6000, int(raw)))
    except ValueError:
        return 2600


def _compact_prompt_text(text: str, *, cap: int) -> str:
    s = (text or "").strip()
    if len(s) <= cap:
        return s
    head = int(cap * 0.7)
    tail = max(120, cap - head - 16)
    return s[:head].rstrip() + "\n\n...(省略)...\n\n" + s[-tail:].lstrip()


def _hard_constraints_block(body_sec: float, *, has_visual: bool, has_transcript: bool) -> str:
    hook_only = os.getenv("HONGGUO_AI_HOOK_ONLY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    try:
        from hook_timeline import story_first_edit_enabled

        story_first = (not hook_only) and story_first_edit_enabled() and has_transcript
    except ImportError:
        story_first = False
    visual_rule = (
        "无对白/对白稀少时段须采用 Brief 中「程序视听选段」的 trim/duration；"
        "有对白段用对白表 start_sec/end_sec。"
        if has_visual and has_transcript
        else (
            "至少 3 段 clip 的 trim 须落在「画面/音效高能轴」某段的 start_sec~end_sec 内，"
            "reason 注明 tags（如 motion/fight/sfx_high）。"
            if has_visual
            else "优先选剧情高能段，避免纯静态对话。"
        )
    )
    tx_rule = (
        "有对白的 clip 须在 reason 写 start_sec/end_sec，duration 须覆盖到 end_sec+0.4s。"
        if has_transcript
        else ""
    )
    try:
        from hook_timeline import ai_script_duration_guidance

        duration_cap = ai_script_duration_guidance()
    except ImportError:
        duration_cap = "正片 clips 合计不超过 23 秒，21–23 秒为最佳。"
    if story_first:
        return f"""
【硬约束 · 故事完整优先（完整对白表已提供）】
1. body duration_sec == sum(clips[].duration_sec)（±0.5）；{duration_cap} **禁止**为控时长短句或删 A 类/反转/尾钩。
2. {visual_rule}
3. {tx_rule}
4. 须覆盖六步叙事各环节；大跳剪标「跳剪」或加 B| 过渡。
5. 每条 reason 以 A| 或 B| 开头；hook_summary 写全六步闭环。
""".strip()
    return f"""
【硬约束 · 全钩子剪辑（不追求剧情完整）】
1. sum(clips[].duration_sec) == body_segments[].duration_sec（误差 ≤0.5）；{duration_cap} 参考约 {body_sec:.0f}s。
2. {visual_rule}
3. {tx_rule}
4. 只保留：诱惑/擦边/悬念/反转/眼神/冲突/高光；其余铺垫、交代、日常对白一律删除。
5. clips 最多 7 段，每段 2.5–5.5s，正片合计不得超过上限；禁止 0 秒段与超长单段。
6. 每条 reason 以 A| 或 B| 开头（≤36 字），并写 tags；hook_summary 必须为**一句话**字符串。
""".strip()


def _opening_prompt_block() -> str:
    if ai_editor_autonomy_enabled():
        return creative_editor_autonomy_block()
    return opening_hook_pattern_block()


def _hook_summary_from_raw(raw: dict[str, Any]) -> str:
    hs = raw.get("hook_summary") or raw.get("summary") or ""
    if isinstance(hs, list):
        return "；".join(str(x).strip() for x in hs if str(x).strip())[:240]
    return str(hs).strip()[:240]


def _clamp_body_segments_to_ai_script_max(segments: list[BodySegmentPlan]) -> None:
    """专业 60s：多集正片合计超过 AI 上限时按比例缩 clips。"""
    if not segments:
        return
    try:
        from hook_timeline import ai_body_script_max_sec, pro_60_template_enabled
    except ImportError:
        return
    if not pro_60_template_enabled():
        return
    mx = ai_body_script_max_sec()
    total = sum(s.duration_sec for s in segments)
    if total <= mx + 0.5:
        logger.info(
            "正片按 AI 分镜：%d 集 clips 合计 %.1fs（上限 %.0fs）",
            len(segments),
            total,
            mx,
        )
        return
    ratio = mx / max(total, 0.01)
    for seg in segments:
        seg.duration_sec = max(MIN_PROMO_BODY_SEC, seg.duration_sec * ratio)
        if seg.clips:
            scale_clips_to_episode_duration(seg.clips, seg.duration_sec)
            _, seg_total, seg.clips = sync_segment_from_clips(
                trim_start_sec=seg.trim_start_sec,
                duration_sec=seg.duration_sec,
                clips=seg.clips,
            )
            seg.duration_sec = seg_total
    new_total = sum(s.duration_sec for s in segments)
    logger.warning(
        "AI 正片合计 %.1fs 超过上限 %.0fs，已缩至 %.1fs",
        total,
        mx,
        new_total,
    )


def _log_ai_clip_duration_check(
    clips: list[ClipFragment], target_sec: float, *, episode_index: int
) -> None:
    if not clips:
        return
    total = sum(c.duration_sec for c in clips)
    try:
        from hook_timeline import story_first_edit_enabled

        if story_first_edit_enabled():
            logger.info(
                "第%d集 AI 正片 clips 合计 %.1fs（故事优先，不以 %.0fs 为目标）",
                episode_index,
                total,
                target_sec,
            )
            return
    except ImportError:
        pass
    if abs(total - target_sec) > 0.8:
        logger.warning(
            "第%d集 AI clips 时长合计 %.1fs ≠ 参考 %.1fs",
            episode_index,
            total,
            target_sec,
        )

MAX_PROMO_BODY_SEC = 120.0
MIN_PROMO_BODY_SEC = 15.0
MAX_MEME_BODY_SEC = 55.0
MIN_MEME_BODY_SEC = 12.0
DEFAULT_OPENING_SEC = 3.0
DEFAULT_OUTRO_SEC = 4.0
@dataclass
class BodySegmentPlan:
    episode_index: int
    trim_start_sec: float = 0.0
    duration_sec: float = 60.0
    label: str = ""
    reason: str = ""
    clips: list[ClipFragment] = field(default_factory=list)
    clips_dialogue_full: list[ClipFragment] = field(default_factory=list)
    meme_captions: list[MemeCaption] = field(default_factory=list)
    meme_beats: list[MemeBeat] = field(default_factory=list)

    def resolved_clips(self) -> list[ClipFragment]:
        if self.clips:
            return list(self.clips)
        if self.duration_sec > 0.5:
            return [
                ClipFragment(
                    trim_start_sec=self.trim_start_sec,
                    duration_sec=self.duration_sec,
                    reason=self.reason,
                )
            ]
        return []


@dataclass
class HookEditPlan:
    opening_text: str
    opening_seconds: float
    outro_keyword: str
    outro_seconds: float
    body_segments: list[BodySegmentPlan] = field(default_factory=list)
    hook_summary: str = ""
    subtitle_hint: str = ""
    post_caption: str = ""
    commentary_lines: list[str] = field(default_factory=list)
    edit_style: str = "meme"  # meme | promo
    meme_captions: list[MemeCaption] = field(default_factory=list)
    meme_beats: list[MemeBeat] = field(default_factory=list)

    def body_for_index(self, index: int) -> Optional[BodySegmentPlan]:
        for seg in self.body_segments:
            if seg.episode_index == index:
                return seg
        return None


def plan_has_dialogue_full_variant(plan: HookEditPlan) -> bool:
    from edge_tts_narration import dialogue_dual_min_gap_sec

    min_gap = dialogue_dual_min_gap_sec()
    for seg in plan.body_segments:
        if not seg.clips_dialogue_full:
            continue
        comp = sum(c.duration_sec for c in seg.clips)
        full = sum(c.duration_sec for c in seg.clips_dialogue_full)
        if full - comp >= min_gap - 0.05:
            return True
    return False


def hook_plan_clip_variant(plan: HookEditPlan, variant: str = "compressed") -> HookEditPlan:
    """variant: compressed（默认）| full（未压缩对白对齐）。"""
    import copy

    if variant == "full":
        if not plan_has_dialogue_full_variant(plan):
            return plan
        out = copy.deepcopy(plan)
        for seg in out.body_segments:
            seg.clips = copy.deepcopy(seg.clips_dialogue_full)
            trim, duration, clips = sync_segment_from_clips(
                trim_start_sec=seg.trim_start_sec,
                duration_sec=seg.duration_sec,
                clips=seg.clips,
            )
            seg.trim_start_sec = trim
            seg.duration_sec = duration
            seg.clips = clips
        return out
    return plan


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _caption_dict(c: MemeCaption) -> dict:
    return {
        "text": c.text,
        "at_sec": c.at_sec,
        "duration_sec": c.duration_sec,
        "position": c.position,
        "style": c.style,
    }


def _beat_dict(b: MemeBeat) -> dict:
    return {
        "at_sec": b.at_sec,
        "effect": b.effect,
        "intensity": b.intensity,
        "duration_sec": b.duration_sec,
    }


def plan_to_dict(plan: HookEditPlan) -> dict:
    return {
        "edit_style": plan.edit_style,
        "meme_captions": [_caption_dict(c) for c in plan.meme_captions],
        "meme_beats": [_beat_dict(b) for b in plan.meme_beats],
        "opening_text": plan.opening_text,
        "opening_seconds": plan.opening_seconds,
        "outro_keyword": plan.outro_keyword,
        "outro_seconds": plan.outro_seconds,
        "hook_summary": plan.hook_summary,
        "subtitle_hint": plan.subtitle_hint,
        "post_caption": plan.post_caption,
        "commentary_lines": plan.commentary_lines,
        "body_segments": [
            {
                "episode_index": s.episode_index,
                "trim_start_sec": s.trim_start_sec,
                "duration_sec": s.duration_sec,
                "label": s.label,
                "reason": s.reason,
                "clips": [
                    {
                        "trim_start_sec": c.trim_start_sec,
                        "duration_sec": c.duration_sec,
                        "reason": c.reason,
                    }
                    for c in s.clips
                ],
                "meme_captions": [_caption_dict(c) for c in s.meme_captions],
                "meme_beats": [_beat_dict(b) for b in s.meme_beats],
            }
            for s in plan.body_segments
        ],
    }


def apply_fixed_opening_to_plan(plan: HookEditPlan) -> None:
    """仅保留 env 配置的片头口播，清空其它解说句。"""
    try:
        from hook_timeline import fixed_opening_line, pro_60_template_enabled

        if pro_60_template_enabled():
            plan.opening_text = fixed_opening_line()
            plan.commentary_lines = []
            return
    except ImportError:
        pass
    fixed = fixed_opening_text()
    if fixed:
        plan.opening_text = fixed
        plan.commentary_lines = []


def _enforce_head_keep_tail_ai(segments: list[BodySegmentPlan]) -> None:
    """
    固定首段结构：前 keep_sec 秒保留原片，后 ai_sec 秒交给 AI/程序高光。
    默认关闭（keep=0）；开启后主要用于 30 秒强钩子（10+20）。
    """
    try:
        keep_sec = max(0.0, float(os.getenv("HONGGUO_KEEP_HEAD_SEC", "0") or 0))
    except ValueError:
        keep_sec = 0.0
    if keep_sec < 0.2 or not segments:
        return
    try:
        ai_sec = max(0.0, float(os.getenv("HONGGUO_AI_TAIL_SEC", "20") or 20))
    except ValueError:
        ai_sec = 20.0
    total_target = max(keep_sec, keep_sec + ai_sec)

    seg = segments[0]
    content_start = 0.0
    if os.getenv("HONGGUO_KEEP_HEAD_FROM_CONTENT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    ):
        # 优先从首句有效对白开始，避开片头 logo/过渡。
        cues = (os.getenv("_HONGGUO_EP1_FIRST_CUE_START", "") or "").strip()
        if cues:
            try:
                content_start = max(0.0, float(cues) - 0.15)
            except ValueError:
                content_start = 0.0
    tail_target = max(0.6, total_target - keep_sec)
    tail_clips = [c for c in (seg.clips or [])]
    if not tail_clips:
        tail_clips = [
            ClipFragment(
                trim_start_sec=content_start + keep_sec,
                duration_sec=tail_target,
                reason="A|后段AI高光",
            )
        ]
    for c in tail_clips:
        c.trim_start_sec = max(float(c.trim_start_sec), content_start + keep_sec)
    tail_total = sum(max(0.1, float(c.duration_sec)) for c in tail_clips)
    ratio = tail_target / max(0.1, tail_total)
    for c in tail_clips:
        c.duration_sec = max(0.4, float(c.duration_sec) * ratio)
    head = ClipFragment(
        trim_start_sec=content_start,
        duration_sec=keep_sec,
        reason=f"A|保留开场前{int(round(keep_sec))}秒正片内容",
    )
    merged = [head] + sorted(tail_clips, key=lambda x: x.trim_start_sec)
    trim, dur, merged = sync_segment_from_clips(
        trim_start_sec=0.0,
        duration_sec=total_target,
        clips=merged,
    )
    seg.trim_start_sec = trim
    seg.duration_sec = dur
    seg.clips = merged
    logger.info(
        "首段结构固定：前%.1fs保留原片 + 后%.1fs AI高光",
        keep_sec,
        tail_target,
    )


def _title_short(drama_title: str, max_len: int = 14) -> str:
    title = (drama_title or "短剧").strip()
    return title[:max_len] if len(title) > max_len else title


def default_plan(
    *,
    drama_title: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
) -> HookEditPlan:
    short = _title_short(drama_title)
    opening_text = (
        fixed_opening_text()
        or (opening or "").strip()
        or f"第1集就反转？《{short}》别划走"
    )
    outro_keyword = (keyword or "").strip() or short
    ep_n = len(episode_labels)
    multi_budget = hook_budget_enabled(ep_n)
    per_ep = per_episode_target_sec(ep_n) if multi_budget else 0.0
    segments: list[BodySegmentPlan] = []
    rng = random.Random(hash(f"{short}:{ep_n}"))
    for i, (label, dur) in enumerate(zip(episode_labels, episode_durations)):
        dur_f = float(dur or 120)
        trim = 8.0 if i == 0 and dur_f > 40 else max(0.0, 5.0 if multi_budget else 0.0)
        trim = max(0.0, trim + rng.uniform(-2.0, 4.0))
        if multi_budget:
            cap = per_ep * (1.08 if i == 0 else 1.0)
            use = min(dur_f - trim, cap)
            min_sec, max_sec = max(16.0, per_ep * 0.75), min(48.0, per_ep * 1.2)
            reason = f"多集钩子：本集约 {per_ep:.0f}s 快切"
        elif meme_edit_enabled():
            use = min(dur_f - trim, 42.0 if i == 0 else 32.0)
            min_sec, max_sec = MIN_MEME_BODY_SEC, MAX_MEME_BODY_SEC
            reason = "Meme 默认：快切高潮，短平快"
        else:
            use = min(dur_f - trim, 75.0 if i == 0 else 55.0)
            min_sec, max_sec = MIN_PROMO_BODY_SEC, MAX_PROMO_BODY_SEC
            reason = "默认：跳过铺垫，保留高潮段"
        ep_dur = _clamp(use, min_sec, max_sec)
        ep_clips: list[ClipFragment] = []
        if multi_clip_enabled():
            ep_clips = build_default_clips(
                dur_f, ep_dur, rng, episode_index=i + 1
            )
            trim, ep_dur, ep_clips = sync_segment_from_clips(
                trim_start_sec=trim,
                duration_sec=ep_dur,
                clips=ep_clips,
            )
            reason = (
                f"快切 {len(ep_clips)} 段共 {ep_dur:.0f}s"
                if ep_clips
                else reason
            )
        seg = BodySegmentPlan(
            episode_index=i + 1,
            trim_start_sec=trim,
            duration_sec=ep_dur,
            label=label,
            reason=reason,
            clips=ep_clips,
        )
        if meme_edit_enabled() and meme_on_body_enabled():
            seg.meme_captions = default_meme_captions(drama_title, i + 1)
            seg.meme_beats = default_meme_beats(seg.duration_sec)
        segments.append(seg)
    post = safe_post_caption(short)
    if multi_budget:
        scale_body_segments_to_budget(segments, episode_count=ep_n)
        for seg in segments:
            if seg.clips:
                scale_clips_to_episode_duration(seg.clips, seg.duration_sec)
                _, total, seg.clips = sync_segment_from_clips(
                    trim_start_sec=seg.trim_start_sec,
                    duration_sec=seg.duration_sec,
                    clips=seg.clips,
                )
                seg.duration_sec = total
    if authentic_preservation_enabled():
        commentary = []
        hook_summary = "剪辑大师模式：只留最精彩片段，原声 1x"
        if multi_budget:
            clip_note = (
                f"高能快切（{hook_arc_hint_text()}）"
                if multi_clip_enabled()
                else "连续裁剪"
            )
            hook_summary = (
                f"剪辑大师：{ep_n} 集仅留最精彩镜头，合集 {hook_duration_range_text()}，"
                f"正片 {sum(s.duration_sec for s in segments):.0f}s，{clip_note}"
            )
        edit_style = "authentic"
    elif meme_edit_enabled():
        commentary = (
            [
                "不是吧？？？",
                "这波直接封神",
                "关注看全集" if douyin_safe_enabled() else f"红果搜「{short}」别停",
            ]
            if meme_on_body_enabled()
            else []
        )
        hook_summary = (
            "Meme 默认：梗字幕 + 卡点缩放 + 快节奏"
            if meme_on_body_enabled()
            else "Meme 片头片尾 + 正片原味"
        )
        edit_style = "meme"
    else:
        commentary = [
            f"《{short}》高能预警",
            "这反转谁想得到？",
            "关注看全集" if douyin_safe_enabled() else f"红果搜「{short}」继续看",
        ]
        hook_summary = "规则默认：短钩子片头 + 高潮正片裁剪"
        edit_style = "promo"
    plan = HookEditPlan(
        opening_text=opening_text,
        opening_seconds=DEFAULT_OPENING_SEC,
        outro_keyword=outro_keyword,
        outro_seconds=DEFAULT_OUTRO_SEC,
        body_segments=segments,
        hook_summary=hook_summary,
        subtitle_hint="" if douyin_safe_enabled() else f"红果搜 {short}",
        commentary_lines=commentary,
        edit_style=edit_style,
    )
    _enforce_head_keep_tail_ai(plan.body_segments)
    apply_fixed_opening_to_plan(plan)
    return plan


def plan_from_manual_dict(
    raw: dict[str, Any],
    *,
    drama_title: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
) -> HookEditPlan:
    """使用者手動填寫的多段剪輯方案：保留入點，時長對齊鉤子預算（如 30s）。"""
    plan = _normalize_plan(
        raw,
        drama_title=drama_title,
        opening=opening,
        keyword=keyword,
        episode_labels=episode_labels,
        episode_durations=episode_durations,
        episode_transcripts=None,
        episode_edit_briefs=None,
        manual=True,
    )
    ep_n = len(episode_labels) or len(plan.body_segments) or 1
    from manual_edit_plan import apply_manual_hook_clip_budget

    body_total = apply_manual_hook_clip_budget(
        plan.body_segments, episode_count=ep_n
    )
    if not (plan.hook_summary or "").strip():
        plan.hook_summary = "手動多段剪輯"
    try:
        from hook_duration_budget import hook_duration_range_text

        summary = plan.hook_summary or ""
        if body_total > 0 and "成片" not in summary:
            base = summary.split("（")[0].strip() or "手動多段剪輯"
            plan.hook_summary = (
                f"{base}（正片约 {body_total:.0f}s，成片 {hook_duration_range_text()}）"
            )
    except ImportError:
        pass
    import logging

    log = logging.getLogger(__name__)
    for seg in plan.body_segments:
        clips = seg.resolved_clips()
        if clips:
            log.info(
                "手動方案 第%d集 %d 段: %s",
                seg.episode_index,
                len(clips),
                ", ".join(
                    f"{c.trim_start_sec:.1f}s+{c.duration_sec:.1f}s" for c in clips
                ),
            )
    return plan


def _normalize_plan(
    raw: dict[str, Any],
    *,
    drama_title: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
    episode_transcripts: Optional[dict[int, list]] = None,
    episode_edit_briefs: Optional[dict] = None,
    manual: bool = False,
) -> HookEditPlan:
    short = _title_short(drama_title)
    opening_text = (
        fixed_opening_text()
        or str(raw.get("opening_text") or opening).strip()
        or (opening or "").strip()
        or f"《{short}》这集信息量太大了"
    )
    outro_keyword = (
        str(raw.get("outro_keyword") or raw.get("keyword") or keyword).strip()
        or (keyword or "").strip()
        or short
    )
    post_caption = str(
        raw.get("post_caption") or raw.get("caption") or raw.get("tweet") or ""
    ).strip()
    opening_seconds = _clamp(
        float(raw.get("opening_seconds") or DEFAULT_OPENING_SEC), 2.0, 6.0
    )
    outro_seconds = _clamp(
        float(raw.get("outro_seconds") or DEFAULT_OUTRO_SEC), 3.0, 8.0
    )

    segments: list[BodySegmentPlan] = []
    raw_segments = raw.get("body_segments") or raw.get("episodes") or []
    if isinstance(raw_segments, list) and raw_segments:
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            idx = int(item.get("episode_index") or item.get("index") or 0)
            if idx < 1:
                continue
            dur_avail = (
                episode_durations[idx - 1]
                if idx - 1 < len(episode_durations)
                else 120.0
            )
            label = str(item.get("label") or "")
            if not label and idx - 1 < len(episode_labels):
                label = episode_labels[idx - 1]
            raw_clips = normalize_clip_list(
                item.get("clips") or item.get("fragments") or item.get("cuts"),
                preserve_manual=manual,
            )
            if manual:
                from multi_clip import clamp_clips_to_source

                raw_clips = clamp_clips_to_source(raw_clips, dur_avail=dur_avail)
                if not raw_clips:
                    continue
                trim, duration, raw_clips = sync_segment_from_clips(
                    trim_start_sec=0.0,
                    duration_sec=sum(c.duration_sec for c in raw_clips),
                    clips=raw_clips,
                )
                segments.append(
                    BodySegmentPlan(
                        episode_index=idx,
                        trim_start_sec=trim,
                        duration_sec=duration,
                        label=label,
                        reason=str(item.get("reason") or "手動剪輯"),
                        clips=raw_clips,
                        clips_dialogue_full=[],
                        meme_captions=[],
                        meme_beats=[],
                    )
                )
                continue
            trim_start = _clamp(
                float(item.get("trim_start_sec") or item.get("start_sec") or 0),
                0.0,
                max(0.0, dur_avail - MIN_PROMO_BODY_SEC),
            )
            duration = float(
                item.get("duration_sec") or item.get("duration") or 60
            )
            if duration < 3.0:
                continue
            speed = body_playback_speed()
            max_source = max(0.0, dur_avail - trim_start)
            max_allowed = max(MIN_PROMO_BODY_SEC, max_source / speed)
            min_body = MIN_MEME_BODY_SEC if meme_edit_enabled() else MIN_PROMO_BODY_SEC
            max_body = MAX_MEME_BODY_SEC if meme_edit_enabled() else MAX_PROMO_BODY_SEC
            try:
                from hook_timeline import ai_body_script_max_sec, pro_60_template_enabled

                if pro_60_template_enabled() and not meme_edit_enabled():
                    max_body = min(max_body, ai_body_script_max_sec())
            except ImportError:
                pass
            duration = _clamp(duration, min_body, min(max_body, max_allowed))
            clips_dialogue_full: list[ClipFragment] = []
            if raw_clips:
                raw_clips = enforce_hook_only_clips(
                    raw_clips,
                    target_sec=duration,
                    dur_avail=dur_avail,
                )
            if raw_clips:
                raw_clips = dedupe_narrative_clips(raw_clips)
            ep_cues_pre = (episode_transcripts or {}).get(idx) or []
            if raw_clips:
                raw_clips = polish_clips_narrative_coherence(
                    raw_clips, ep_cues_pre, dur_avail=dur_avail
                )
                try:
                    from hook_timeline import story_first_edit_enabled

                    arc_target = 0.0 if story_first_edit_enabled() else duration
                except ImportError:
                    arc_target = duration
                raw_clips = polish_arc_pacing_clips(
                    raw_clips,
                    ep_cues_pre,
                    dur_avail=dur_avail,
                    target_sec=arc_target,
                )
            brief = (episode_edit_briefs or {}).get(idx)
            from hook_timeline import ai_body_faithful_enabled

            if (
                raw_clips
                and brief
                and multi_clip_enabled()
                and not ai_body_faithful_enabled()
            ):
                raw_clips = enforce_opening_from_beats(
                    raw_clips,
                    brief.suggested_beats,
                    dur_avail=dur_avail,
                )
            clip_rng = random.Random(f"{drama_title}:{idx}:{dur_avail:.0f}")
            if not raw_clips and multi_clip_enabled():
                raw_clips = build_default_clips(
                    dur_avail, duration, clip_rng, episode_index=idx
                )
            elif raw_clips and multi_clip_enabled():
                ep_cues = (episode_transcripts or {}).get(idx) or []
                if ai_clip_strict_enabled():
                    _log_ai_clip_duration_check(
                        raw_clips, duration, episode_index=idx
                    )
                    clips_dialogue_full: list[ClipFragment] = []
                    base_clips = list(raw_clips)
                    faithful = ai_body_faithful_enabled()
                    if faithful:
                        if brief and brief.program_visual_clips:
                            raw_clips = enforce_program_visual_opening(
                                raw_clips,
                                brief.program_visual_clips,
                                ep_cues,
                                dur_avail=dur_avail,
                            )
                        if ep_cues:
                            raw_clips = extend_clips_to_reason_span(
                                base_clips, ep_cues, dur_avail=dur_avail
                            )
                            from multi_clip import enforce_clip_duration_floor

                            raw_clips = enforce_clip_duration_floor(
                                raw_clips, dur_avail, cues=ep_cues
                            )
                        else:
                            from multi_clip import enforce_clip_duration_floor

                            raw_clips = enforce_clip_duration_floor(
                                base_clips, dur_avail, cues=None
                            )
                        clip_total = sum(c.duration_sec for c in raw_clips)
                        raw_clips = apply_ai_clips_strict(
                            raw_clips,
                            dur_avail=dur_avail,
                            target_sec=clip_total or duration,
                            preserve_duration=True,
                        )
                    elif ep_cues:
                        from edge_tts_narration import dual_dialogue_export_enabled
                        from video_transcript import (
                            align_clips_dialogue_variants,
                            align_clips_to_transcript,
                        )

                        if dual_dialogue_export_enabled():
                            raw_clips, full_clips = align_clips_dialogue_variants(
                                base_clips, ep_cues, target_sec=duration
                            )
                            if full_clips:
                                full_target = sum(
                                    c.duration_sec for c in full_clips
                                )
                                clips_dialogue_full = apply_ai_clips_strict(
                                    full_clips,
                                    dur_avail=dur_avail,
                                    target_sec=full_target,
                                    preserve_duration=True,
                                )
                        else:
                            extended = extend_clips_to_reason_span(
                                base_clips, ep_cues, dur_avail=dur_avail
                            )
                            raw_clips = align_clips_to_transcript(
                                extended, ep_cues, target_sec=duration
                            )
                        raw_clips = rebalance_clips_front_heavy(
                            raw_clips,
                            dur_avail=dur_avail,
                            target_sec=duration,
                        )
                        raw_clips = apply_ai_clips_strict(
                            raw_clips,
                            dur_avail=dur_avail,
                            target_sec=duration,
                        )
                    else:
                        from multi_clip import enforce_clip_duration_floor

                        raw_clips = enforce_clip_duration_floor(
                            base_clips, dur_avail
                        )
                        raw_clips = rebalance_clips_front_heavy(
                            raw_clips,
                            dur_avail=dur_avail,
                            target_sec=duration,
                        )
                        raw_clips = apply_ai_clips_strict(
                            raw_clips,
                            dur_avail=dur_avail,
                            target_sec=duration,
                        )
                else:
                    raw_clips = refine_clips_for_hook_arc(
                        raw_clips,
                        dur_avail=dur_avail,
                        target_sec=duration,
                        rng=clip_rng,
                        episode_index=idx,
                    )
                    ep_cues = (episode_transcripts or {}).get(idx) or []
                    if ep_cues:
                        raw_clips = _snap_clips_to_transcript(
                            raw_clips, ep_cues, dur_avail=dur_avail
                        )
                    before = len(raw_clips)
                    raw_clips = ensure_clips_count(
                        raw_clips,
                        dur_avail=dur_avail,
                        target_sec=duration,
                        rng=clip_rng,
                        episode_index=idx,
                    )
                    if len(raw_clips) > before:
                        logger.info(
                            "第%d集 AI 仅 %d 段，已补至 %d 段快切",
                            idx,
                            before,
                            len(raw_clips),
                        )
            trim, duration, raw_clips = sync_segment_from_clips(
                trim_start_sec=trim_start,
                duration_sec=duration,
                clips=raw_clips,
            )
            if raw_clips and not ai_clip_strict_enabled():
                scale_clips_to_episode_duration(raw_clips, duration)
                trim, duration, raw_clips = sync_segment_from_clips(
                    trim_start_sec=trim,
                    duration_sec=duration,
                    clips=raw_clips,
                )
            seg_caps = normalize_meme_captions(
                item.get("meme_captions") or item.get("captions")
            )
            seg_beats = normalize_meme_beats(item.get("meme_beats") or item.get("beats"))
            segments.append(
                BodySegmentPlan(
                    episode_index=idx,
                    trim_start_sec=trim,
                    duration_sec=duration,
                    label=label,
                    reason=str(item.get("reason") or ""),
                    clips=raw_clips,
                    clips_dialogue_full=clips_dialogue_full,
                    meme_captions=seg_caps,
                    meme_beats=seg_beats,
                )
            )

    if not segments:
        plan = default_plan(
            drama_title=drama_title,
            opening=opening_text,
            keyword=outro_keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
        )
        if post_caption:
            plan.post_caption = post_caption
        return plan

    commentary_lines: list[str] = []
    raw_commentary = raw.get("commentary_lines") or raw.get("overlay_lines") or []
    if isinstance(raw_commentary, list):
        commentary_lines = [
            str(x).strip() for x in raw_commentary if str(x).strip()
        ][:6]

    if authentic_preservation_enabled():
        edit_style = "authentic"
    else:
        edit_style = str(
            raw.get("edit_style") or ("meme" if meme_edit_enabled() else "promo")
        ).lower()
        if edit_style not in ("meme", "promo", "authentic"):
            edit_style = "meme" if meme_edit_enabled() else "promo"

    plan_caps = normalize_meme_captions(raw.get("meme_captions") or raw.get("meme_overlays"))
    plan_beats = normalize_meme_beats(raw.get("meme_beats"))

    for seg in segments:
        if not seg.meme_captions and plan_caps and meme_on_body_enabled():
            seg.meme_captions = plan_caps[:3]
        if not seg.meme_beats and plan_beats and meme_on_body_enabled():
            seg.meme_beats = plan_beats[:2]
        if meme_edit_enabled() and meme_on_body_enabled() and not seg.meme_captions:
            seg.meme_captions = default_meme_captions(drama_title, seg.episode_index)
        if meme_edit_enabled() and meme_on_body_enabled() and not seg.meme_beats:
            seg.meme_beats = default_meme_beats(seg.duration_sec)

    if authentic_preservation_enabled() or not meme_on_body_enabled():
        for seg in segments:
            seg.meme_captions = []
            seg.meme_beats = []
        plan_caps = []
        plan_beats = []
        commentary_lines = []

    if meme_edit_enabled() and meme_on_body_enabled() and segments and not commentary_lines:
        commentary_lines = [c.text for seg in segments for c in seg.meme_captions[:3]]

    ep_n = len(episode_labels) or len(segments)
    from hook_timeline import ai_body_faithful_enabled

    if (
        not manual
        and hook_budget_enabled(ep_n)
        and segments
        and not ai_body_faithful_enabled()
    ):
        scale_body_segments_to_budget(segments, episode_count=ep_n)
        for seg in segments:
            if seg.clips:
                scale_clips_to_episode_duration(seg.clips, seg.duration_sec)
                _, total, seg.clips = sync_segment_from_clips(
                    trim_start_sec=seg.trim_start_sec,
                    duration_sec=seg.duration_sec,
                    clips=seg.clips,
                )
                seg.duration_sec = total
    elif segments and ai_body_faithful_enabled():
        for seg in segments:
            if seg.clips:
                _, total, seg.clips = sync_segment_from_clips(
                    trim_start_sec=seg.trim_start_sec,
                    duration_sec=seg.duration_sec,
                    clips=seg.clips,
                )
                seg.duration_sec = total
        _clamp_body_segments_to_ai_script_max(segments)

    if not fixed_opening_text():
        opening_text = sanitize_promo_copy(opening_text, max_len=60) or opening_text
    subtitle_hint = sanitize_promo_copy(str(raw.get("subtitle_hint") or ""), max_len=24)
    post_caption = sanitize_promo_copy(post_caption, max_len=200)
    if not post_caption:
        post_caption = safe_post_caption(drama_title)
    commentary_lines = [
        sanitize_promo_copy(x, max_len=36)
        for x in commentary_lines
        if sanitize_promo_copy(x, max_len=36)
    ]
    for seg in segments:
        for cap in seg.meme_captions:
            cap.text = sanitize_promo_copy(cap.text, max_len=24) or cap.text
    for cap in plan_caps:
        cap.text = sanitize_promo_copy(cap.text, max_len=24) or cap.text

    if not manual:
        _keep_first_dialogue_line(segments, episode_transcripts=episode_transcripts)
        _keep_flirt_dialogue_lines(segments, episode_transcripts=episode_transcripts)
        _enforce_head_keep_tail_ai(segments)

    if manual and not (raw.get("hook_summary") or "").strip():
        hook_summary = "手動多段剪輯"
    else:
        hook_summary = _hook_summary_from_raw(raw)

    plan = HookEditPlan(
        opening_text=opening_text,
        opening_seconds=opening_seconds,
        outro_keyword=outro_keyword,
        outro_seconds=outro_seconds,
        body_segments=segments,
        hook_summary=hook_summary,
        subtitle_hint=subtitle_hint,
        post_caption=post_caption,
        commentary_lines=commentary_lines,
        edit_style="manual" if manual else edit_style,
        meme_captions=plan_caps if not manual else [],
        meme_beats=plan_beats if not manual else [],
    )
    apply_fixed_opening_to_plan(plan)
    return plan


def _snap_clips_to_transcript(
    clips: list[ClipFragment],
    cues: list,
    *,
    dur_avail: float,
) -> list[ClipFragment]:
    """将 AI 给出的入点贴近真实对白起点，并尽量覆盖整句台词。"""
    if not clips or not cues:
        return clips
    max_start = max(5.0, float(dur_avail) - 8.0)
    out: list[ClipFragment] = []
    for c in clips:
        trim = max(0.0, min(max_start, float(c.trim_start_sec)))
        best = min(cues, key=lambda cue: abs(float(cue.start_sec) - trim))
        start = max(0.0, float(best.start_sec) - 0.8)
        end = float(best.end_sec) + 0.35
        dur = max(c.duration_sec, end - start, 2.5)
        if cohesive_edit_enabled():
            dur = min(dur, clip_max_sec())
        else:
            dur = min(dur, clip_max_sec())
        out.append(
            ClipFragment(
                trim_start_sec=start,
                duration_sec=dur,
                reason=c.reason or f"A|{best.text[:24]}",
            )
        )
    return out


def _keep_first_dialogue_line(
    segments: list[BodySegmentPlan],
    *,
    episode_transcripts: Optional[dict[int, list]] = None,
) -> None:
    """保留主视频第一句台词：首段首刀锚定到第一句对白起点。"""
    v = os.getenv("HONGGUO_KEEP_FIRST_LINE", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return
    if not segments or not episode_transcripts:
        return

    def _cue_start(c: Any) -> float:
        if hasattr(c, "start_sec"):
            return float(c.start_sec)
        if isinstance(c, dict):
            return float(c.get("start_sec") or 0.0)
        return 0.0

    def _cue_end(c: Any) -> float:
        if hasattr(c, "end_sec"):
            return float(c.end_sec)
        if isinstance(c, dict):
            return float(c.get("end_sec") or 0.0)
        return 0.0

    for seg in segments:
        cues = (episode_transcripts or {}).get(seg.episode_index) or []
        if not cues or not seg.clips:
            continue
        first_cue = None
        for cue in cues:
            st = _cue_start(cue)
            en = _cue_end(cue)
            if en > st + 0.05:
                first_cue = (st, en)
                break
        if not first_cue:
            continue
        st, en = first_cue
        if seg.episode_index == 1:
            os.environ["_HONGGUO_EP1_FIRST_CUE_START"] = f"{st:.3f}"
        first = seg.clips[0]
        if float(first.trim_start_sec) <= st + 0.25:
            continue
        first.trim_start_sec = round(max(0.0, st - 0.15), 2)
        first.duration_sec = round(max(float(first.duration_sec), en - st + 0.55), 2)
        first.reason = (
            f"A|保留首句台词 start_sec={st:.2f} end_sec={en:.2f} "
            + (first.reason or "")
        ).strip()
        trim, duration, synced = sync_segment_from_clips(
            trim_start_sec=seg.trim_start_sec,
            duration_sec=seg.duration_sec,
            clips=seg.clips,
        )
        seg.trim_start_sec = trim
        seg.duration_sec = duration
        seg.clips = synced
        logger.info(
            "第%d集 已锚定首句台词：%.2fs-%.2fs",
            seg.episode_index,
            st,
            en,
        )


def _keep_flirt_dialogue_lines(
    segments: list[BodySegmentPlan],
    *,
    episode_transcripts: Optional[dict[int, list]] = None,
) -> None:
    """保留调戏/暧昧台词片段：若未命中则补一刀进 clips。"""
    v = os.getenv("HONGGUO_KEEP_FLIRT_LINE", "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return
    if not segments or not episode_transcripts:
        return
    kws = ("调戏", "撩", "勾引", "暧昧", "亲", "宝贝", "姐姐", "哥哥")
    force_raw = os.getenv("HONGGUO_FORCE_KEEP_LINES", "").strip()
    force_lines = [x.strip() for x in re.split(r"[,\n|;；]+", force_raw) if x.strip()]

    def _cue_span(c: Any) -> tuple[float, float, str]:
        if hasattr(c, "start_sec"):
            st = float(getattr(c, "start_sec", 0.0) or 0.0)
            en = float(getattr(c, "end_sec", 0.0) or 0.0)
            txt = str(getattr(c, "text", "") or "")
        elif isinstance(c, dict):
            st = float(c.get("start_sec") or 0.0)
            en = float(c.get("end_sec") or 0.0)
            txt = str(c.get("text") or "")
        else:
            return 0.0, 0.0, ""
        return st, en, txt

    for seg in segments:
        cues = (episode_transcripts or {}).get(seg.episode_index) or []
        if not cues or not seg.clips:
            continue
        flirt = None
        # 先命中“强制保留台词白名单”
        if force_lines:
            for cue in cues:
                st, en, txt = _cue_span(cue)
                if en <= st + 0.05:
                    continue
                if any(k in txt for k in force_lines):
                    flirt = (st, en, txt.strip())
                    break
        for cue in cues:
            if flirt:
                break
            st, en, txt = _cue_span(cue)
            if en <= st + 0.05:
                continue
            if any(k in txt for k in kws):
                flirt = (st, en, txt.strip())
                break
        if not flirt:
            continue
        st, en, txt = flirt
        hit = False
        for c in seg.clips:
            cst = float(c.trim_start_sec)
            ced = cst + float(c.duration_sec)
            if not (ced <= st + 0.02 or cst >= en - 0.02):
                hit = True
                break
        if hit:
            continue
        insert = ClipFragment(
            trim_start_sec=round(max(0.0, st - 0.2), 2),
            duration_sec=round(max(1.2, en - st + 0.7), 2),
            reason=f"A|保留调戏台词 start_sec={st:.2f} end_sec={en:.2f}",
        )
        seg.clips = [insert] + list(seg.clips)
        trim, duration, synced = sync_segment_from_clips(
            trim_start_sec=seg.trim_start_sec,
            duration_sec=seg.duration_sec,
            clips=seg.clips,
        )
        seg.trim_start_sec = trim
        seg.duration_sec = duration
        seg.clips = synced
        logger.info(
            "第%d集 已补调戏台词：%s",
            seg.episode_index,
            txt[:20],
        )


def _build_authentic_prompt(
    *,
    drama_title: str,
    drama_intro: str,
    episode_labels: list[str],
    episode_durations: list[float],
    episode_transcripts: Optional[dict[int, list]] = None,
    episode_visual_profiles: Optional[dict[int, list]] = None,
    episode_edit_briefs: Optional[dict] = None,
) -> str:
    eps_lines = []
    for i, (lab, dur) in enumerate(zip(episode_labels, episode_durations), start=1):
        eps_lines.append(f"  - 第{i}集「{lab}」可用时长约 {dur:.0f} 秒")
    eps_block = "\n".join(eps_lines) or "  - （无分集信息）"
    speed = body_playback_speed()
    n = len(episode_labels)
    per = per_episode_target_sec(n) if hook_budget_enabled(n) else 45.0
    try:
        from hook_timeline import (
            ai_script_duration_guidance,
            body_main_sec,
            pro_60_template_enabled,
            timeline_summary,
        )

        pro_tpl = pro_60_template_enabled()
        script_duration_line = ai_script_duration_guidance()
    except ImportError:
        pro_tpl = False
        script_duration_line = "正片 clips 合计不超过 65 秒，50–60 秒为最佳。"

    type_hint = drama_type_hint_for_prompt(drama_title, drama_intro)
    if hook_budget_enabled(n):
        body = body_budget_seconds(n)
        per = per_episode_target_sec(n)
        body_lo = body_budget_seconds(n, total_sec=hook_target_min_sec())
        body_hi = body
        clip_n = clips_per_episode(per) if multi_clip_enabled() else 0
        abc_block = editing_rules_block(
            body_sec=body_main_sec() if pro_tpl else per,
            opening_sec=5.0 if pro_tpl else 3.0,
        )
        if pro_tpl:
            from hook_timeline import ai_body_faithful_enabled

            if ai_body_faithful_enabled():
                duration_rule = (
                    "2. 专业60s：片头黄金口播+片尾 CTA 由系统固定；"
                    "**正片只输出 clips 分镜表**，body duration_sec 必须等于 clips 时长之和（±0.5）。\n"
                    f"{script_duration_line}\n"
                    f"{abc_block}\n"
                    f"{type_hint}\n"
                    f"每集 clips 约 {clip_n} 段；按你判断写 trim_start_sec/duration_sec，"
                    "勿为凑满固定秒数压短对白。\n"
                    f"硬切无转场；原速 {speed:g}x；commentary_lines 为空。"
                )
            else:
                duration_rule = (
                    f"2. 专业60秒：{timeline_summary()}；{script_duration_line}\n"
                    f"{abc_block}\n"
                    f"{type_hint}\n"
                    f"每集 clips 约 {clip_n} 段（"
                    f"{'连贯 7~12s/段、同场景少切镜' if cohesive_edit_enabled() else 'A|2~4s + 少量 B|≤1s'}），"
                    f"硬切无转场；原速 {speed:g}x；"
                    f"commentary_lines 为空；opening_text 由系统口播。"
                )
        elif multi_clip_enabled():
            duration_rule = (
                f"2. 正片 {body_lo:.0f}–{body_hi:.0f}s（{hook_duration_range_text()}）；"
                f"每集≈{per:.0f}s、clips 约 {clip_n} 段。\n"
                f"{abc_block}\n"
                f"{type_hint}\n"
                f"{hook_arc_hint_text()}；倍速 {speed:g}x；尾音约 {clip_tail_pad_sec():.1f}s。"
            )
        else:
            duration_rule = (
                f"2. 共 {n} 集精彩合集：正片合计约 {body_lo:.0f}–{body_hi:.0f} 秒（整条钩子 "
                f"{hook_duration_range_text()}），每集 duration_sec 建议 {per:.0f}±8 秒；"
                f"trim_start_sec 跳过铺垫；成片倍速 {speed:g}x。"
            )
    elif multi_clip_enabled():
        duration_rule = (
            f"2. 每集 clips {clips_per_episode()} 段，{hook_arc_hint_text()}；"
            f"每段 reason 写明高能类型；倍速 {speed:g}x。"
        )
    else:
        duration_rule = (
            f"2. 单集成片 30-60 秒；trim_start_sec 常 8-20 秒；成片倍速 {speed:g}x（duration_sec=成片时长）。"
        )
    from video_moment_profile import visual_block_for_episodes
    from video_transcript import transcript_block_for_episodes

    body_tgt = body_main_sec() if pro_tpl else per
    has_tx = bool(episode_transcripts)
    has_vis = bool(episode_visual_profiles)

    visual_block = (
        visual_block_for_episodes(episode_visual_profiles or {}, episode_labels)
        if has_vis
        else ""
    )
    transcript_block = (
        transcript_block_for_episodes(
            episode_transcripts or {},
            episode_labels,
            body_target_sec=body_tgt,
        )
        if has_tx
        else ""
    )
    hard_block = _hard_constraints_block(
        body_tgt, has_visual=has_vis, has_transcript=has_tx
    )
    impact_brief = human_impact_script_brief(body_sec=body_tgt)
    opening_block = _opening_prompt_block()
    structure_block = briefs_block_for_prompt(episode_edit_briefs or {})
    few_shot_block = few_shot_block_for_prompt(episode_edit_briefs or {}, body_tgt)
    if has_tx or has_vis:
        from hook_timeline import ai_body_faithful_enabled

        if ai_body_faithful_enabled() and pro_tpl:
            from hook_timeline import story_first_edit_enabled

            hook_only = os.getenv("HONGGUO_AI_HOOK_ONLY", "1").strip().lower() not in (
                "0",
                "false",
                "no",
                "off",
            )
            if (not hook_only) and story_first_edit_enabled() and has_tx:
                duration_rule = (
                    "2. **故事完整优先**：从完整对白表+画面轴选出六步叙事所需的全部好情节；"
                    "body duration_sec = clips 之和（±0.5）。\n"
                    f"{script_duration_line}\n"
                    f"{type_hint}\n"
                    "禁止为控时长短句/删反转/删尾钩；每条 clip duration 覆盖对白 end_sec。\n"
                    f"{hard_block}"
                )
            else:
                autonomy_note = (
                    "段数与入点由你按本集自主决定（3~6 段均可），勿套固定 4 段/固定起切秒数。"
                    if ai_editor_autonomy_enabled()
                    else "开篇尽量短、删无对白穿梭；全片叙事连贯。"
                )
                duration_rule = (
                    "2. 正片以 AI clips 为准：结合画面/音效轴+对白表选点；"
                    "body duration_sec = clips 之和（±0.5）。\n"
                    f"{script_duration_line}\n"
                    f"{type_hint}\n"
                    "仅使用已提供的本地素材时间轴（禁止臆测外部片段）；"
                    "只剪诱惑/擦边/悬念/反转/眼神/冲突/高光。"
                    f"{autonomy_note} "
                    "每条 clip duration 须覆盖对白 end_sec。\n"
                    f"{hard_block}"
                )
        else:
            duration_rule = (
                f"2. 正片目标 {body_tgt:.0f}s：结合「画面/音效高能轴」+「对白时间轴」选 clip；"
                f"clips.duration_sec 之和必须等于 {body_tgt:.0f}（±0.5）。\n"
                f"{type_hint}\n"
                "打斗/快切/音效峰段落优先；**前段爽点略多（约占正文 "
                f"{int(early_body_budget_ratio() * 100)}% 内）**，"
                f"后段仍须冲突/铺垫/悬念（合计约 {min_late_body_sec():.0f}–{max_late_body_sec():.0f}s，"
                "避免长身世回忆抢戏）；"
                "**台词必须说完整**（duration 须覆盖对白 end_sec），并遵守正片时长上限。\n"
                f"{hard_block}"
            )
    return f"""{MASTER_EDITOR_PERSONA}
{MASTER_EDITOR_REJECT}

{impact_brief}

{opening_block}

成片要求：正片保留原声对白（不叠解说条）；下方数据表用于写出「对人最友好、最有冲击力」的 clips 脚本。

剧名：{drama_title}
简介：{drama_intro[:500] if drama_intro else "（无）"}
已选分集：
{eps_block}
{structure_block}
{visual_block}
{transcript_block}
{few_shot_block}

请只输出 JSON（不要 markdown）：
{{
  "edit_style": "authentic",
    "opening_text": "1 句短钩子，≤24 字，仅用于片头口播",
  "outro_keyword": "搜索词 8-16 字",
  "post_caption": "发布文案 2-4 行 + 话题",
  "hook_summary": "一句话说明刺激点组合（诱惑/擦边/悬念/反转/眼神/冲突）与尾钩",
  "commentary_lines": [],
  "body_segments": [
    {{
      "episode_index": 1,
      "duration_sec": 23,
      "reason": "须等于 clips.duration_sec 之和；正片聚焦 21–23s 冲突高光台词",
      "clips": [
        {{"trim_start_sec": 0.0, "duration_sec": 6.0, "reason": "A|开场冲突台词 tags=conflict/dialogue_hit start_sec=... end_sec=..."}},
        {{"trim_start_sec": 0.0, "duration_sec": 7.0, "reason": "A|对峙升级/情绪爆发（秒数按本集轴填写）"}},
        {{"trim_start_sec": 0.0, "duration_sec": 6.0, "reason": "A|打脸反转（跳剪须标注）"}},
        {{"trim_start_sec": 0.0, "duration_sec": 4.0, "reason": "A|尾钩悬念/追更点"}}
      ],
      "meme_captions": [],
      "meme_beats": []
    }}
  ]
}}

规则：
1. commentary_lines 必须为空数组；meme_captions、meme_beats 必须为空数组。
{duration_rule}
{ai_compliance_rule_block()}
3. opening_text ≤ 24 字；hook_summary 只写刺激点与尾钩，不要求剧情完整。
4. clips 按时间顺序；reason 以 A|/B| 开头；秒数必须来自本集数据表，勿照抄示例里的 0.0；{
        "自检：各段 duration 相加必须精确等于 body duration_sec"
        if (has_tx or has_vis)
        else (
            "每段 duration_sec 7~" + str(int(clip_max_sec())) + "s"
            if cohesive_edit_enabled()
            else "A 类 2~" + str(int(clip_max_sec())) + "s"
        )
    }。
5. body_segments 覆盖 episode_index 1 到 {n}。"""


def _build_prompt(
    *,
    drama_title: str,
    drama_intro: str,
    episode_labels: list[str],
    episode_durations: list[float],
    episode_transcripts: Optional[dict[int, list]] = None,
    episode_visual_profiles: Optional[dict[int, list]] = None,
    episode_edit_briefs: Optional[dict] = None,
) -> str:
    eps_lines = []
    for i, (lab, dur) in enumerate(zip(episode_labels, episode_durations), start=1):
        eps_lines.append(f"  - 第{i}集「{lab}」可用时长约 {dur:.0f} 秒")
    eps_block = "\n".join(eps_lines) or "  - （无分集信息）"

    if authentic_preservation_enabled():
        return _build_authentic_prompt(
            drama_title=drama_title,
            drama_intro=drama_intro,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
            episode_transcripts=episode_transcripts,
            episode_visual_profiles=episode_visual_profiles,
            episode_edit_briefs=episode_edit_briefs,
        )

    if meme_edit_enabled():
        return f"""{MASTER_EDITOR_PERSONA}
在此基础上用 meme 风（梗字幕/卡点）包装已选出的最精彩片段；{MASTER_EDITOR_REJECT}
根据剧名与简介，设计横屏 16:9 裁剪 + meme 字幕 + 卡点。

剧名：{drama_title}
简介：{drama_intro[:500] if drama_intro else "（无，请根据剧名推断：逆袭/修仙/霸总/虐恋等）"}
已选分集：
{eps_block}

请只输出 JSON（不要 markdown）：
{{
  "edit_style": "meme",
  "opening_text": "1 句 meme 口吻钩子，如：不是吧？？？这也能反转",
  "outro_keyword": "搜索词 8-16 字",
  "post_caption": "发布文案：口语梗 + 评论钩子 + 2-4 个话题",
  "hook_summary": "一句话说明 meme 剪辑策略",
  "commentary_lines": ["与 meme 字幕同文1", "同文2", "同文3"],
  "body_segments": [
    {{
      "episode_index": 1,
      "trim_start_sec": 12,
      "duration_sec": 38,
      "reason": "从冲突/打脸切入，跳过铺垫",
      "meme_captions": [
        {{"text": "前方高能！", "at_sec": 1.2, "duration_sec": 2.2, "position": "top", "style": "punch"}},
        {{"text": "这反转绝了", "at_sec": 10.0, "duration_sec": 2.4, "position": "impact", "style": "shock"}},
        {{"text": "红果搜剧名", "at_sec": 22.0, "duration_sec": 2.0, "position": "bottom", "style": "whisper"}}
      ],
      "meme_beats": [
        {{"at_sec": 8.0, "effect": "zoom_punch", "intensity": 1.14}},
        {{"at_sec": 18.0, "effect": "zoom_punch", "intensity": 1.12}}
      ]
    }}
  ]
}}

规则：
1. meme_captions 每集最多 3 条，每条 6-12 字，口语梗/反问；position 只用 top、impact、bottom（禁止 center）；相邻字幕至少间隔 2 秒；duration_sec 建议 2.0-2.6。
2. commentary_lines 必须与 meme_captions 的 text 完全一致（仅用于配音，画面只显示 meme 大字幕）。
3. meme_beats 每集最多 2 个，落在高能瞬间；effect 用 zoom_punch；intensity 1.10-1.18。
4. 单集成片 20-42 秒；trim_start_sec 常 8-25 秒；成片 1.618 倍速，duration_sec=成片时长。
5. opening_text ≤ 24 字；body_segments 覆盖 episode_index 1 到 {len(episode_labels)}。"""

    return f"""{MASTER_EDITOR_PERSONA}
{MASTER_EDITOR_REJECT}
你是投流编导：在「只留最精彩」的前提下设计横屏 16:9 裁剪，并写发布推文；成片需过平台判重。

剧名：{drama_title}
简介：{drama_intro[:500] if drama_intro else "（无，请根据剧名推断类型：逆袭/修仙/霸总/虐恋等）"}
已选分集：
{eps_block}

请只输出 JSON（不要 markdown）：
{{
  "edit_style": "promo",
  "opening_text": "1-2 行钩子解说，会烧录进 1 秒解说卡",
  "opening_seconds": 0,
  "outro_keyword": "供片尾引导用的搜索词，8-16 字",
  "outro_seconds": 0,
  "post_caption": "可直接发布的推文：2-4 行 + 2-4 个话题标签，口语化、有评论钩子",
  "hook_summary": "一句话说明流量策略",
  "subtitle_hint": "可选关键词",
  "commentary_lines": [
    "正片烧录解说字幕1，8-18字，口语化",
    "正片烧录解说字幕2",
    "正片烧录解说字幕3"
  ],
  "body_segments": [
    {{
      "episode_index": 1,
      "trim_start_sec": 10,
      "duration_sec": 50,
      "reason": "跳过铺垫，从冲突/反转切入"
    }}
  ]
}}

规则：
1. commentary_lines 必须 3-5 条，每条 8-18 字，像真人解说，不要与 thousands 账号相同的套话。
2. 正片单集成片 23 秒左右；trim_start_sec 常 5-25 秒，删除铺垫，仅保留冲突/反转高光。
3. 成片正片默认 1.618 倍速：duration_sec 指成片时长；源片取用约为 duration_sec×1.618。
4. opening_text ≤ 60 字；outro_keyword ≤ 16 字；post_caption ≤ 200 字。
5. body_segments 须覆盖 episode_index 1 到 {len(episode_labels)}。"""


async def plan_hook_edit(
    client: httpx.AsyncClient,
    *,
    drama_title: str,
    drama_intro: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
    episode_transcripts: Optional[dict[int, list]] = None,
    episode_visual_profiles: Optional[dict[int, list]] = None,
    episode_keyframes: Optional[dict[int, list]] = None,
    episode_edit_briefs: Optional[dict] = None,
) -> HookEditPlan:
    """调用 LLM 生成剪辑方案；失败则回退 default_plan。"""
    if not is_configured():
        logger.info("未配置 LLM，使用默认剪辑方案")
        plan = default_plan(
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
        )
        plan.hook_summary = (
            "未配置 LLM：本地请启动 LM Studio；云端请在 .env 填写 QWEN_API_KEY"
        )
        return plan

    fallback = default_plan(
        drama_title=drama_title,
        opening=opening,
        keyword=keyword,
        episode_labels=episode_labels,
        episode_durations=episode_durations,
    )

    has_transcript = bool(episode_transcripts)
    has_visual = bool(episode_visual_profiles)
    try:
        from hook_timeline import body_main_sec

        body_pick_sec = body_main_sec()
    except ImportError:
        body_pick_sec = 50.0

    from video_transcript import transcript_edit_required

    if transcript_edit_required() and not has_transcript:
        logger.warning(
            "未获取对白时间轴（请安装 faster-whisper 或确认片源含字幕），"
            "AI 无法按台词选 ~%.0fs 片段",
            body_pick_sec,
        )
    prompt = _build_prompt(
        drama_title=drama_title,
        drama_intro=drama_intro,
        episode_labels=episode_labels,
        episode_durations=episode_durations,
        episode_transcripts=episode_transcripts,
        episode_visual_profiles=episode_visual_profiles,
        episode_edit_briefs=episode_edit_briefs,
    )
    if has_visual or has_transcript:
        try:
            from hook_timeline import story_first_edit_enabled

            from hook_timeline import ai_script_duration_guidance

            duration_cap = ai_script_duration_guidance()
            if story_first_edit_enabled():
                clip_hint = (
                    " 已提供完整对白表+画面/音效轴："
                    "**故事完整优先**，选出六步叙事所需的全部好情节；"
                    f"{duration_cap} "
                    "body duration_sec = clips 之和，勿为控时长短句或删反转/尾钩；"
                    "每条 duration 覆盖对白 end_sec；reason 写 tags 与 start_sec/end_sec。"
                )
            else:
                clip_hint = (
                    f" 已提供画面/音效高能轴"
                    f"{' + 对白表' if has_transcript else ''}：按本集自主选高光与简版叙事，"
                    f"{duration_cap} "
                    f"参考约 {body_pick_sec:.0f}s；"
                    "只从本地素材中选诱惑/擦边/悬念/反转/眼神/冲突/高光片段；"
                    "clips.duration_sec 之和 = body duration_sec（±0.5）；"
                    "不要求剧情完整与叙事闭环，只追求钩子强度；"
                    "reason 建议标环节并写 tags、start_sec/end_sec。"
                )
        except ImportError:
            clip_hint = (
                f" 已提供画面/音效高能轴"
                f"{' + 对白表' if has_transcript else ''}："
                f"参考约 {body_pick_sec:.0f}s；"
                "clips.duration_sec 之和 = body duration_sec（±0.5）。"
            )
    elif multi_clip_enabled():
        clip_hint = (
            " clips 弧线：冲突→打脸→反转→悬念；reason 必填高能类型；"
            "每段 duration_sec 宁长勿短，禁止对白说到一半就切。"
        )
    else:
        clip_hint = " 删除铺垫，只留高潮与反转。"
    from qwen_client import is_likely_vision_model
    from video_keyframes import (
        build_edit_vision_user_content,
        vision_effective_max_for_api,
        vision_frames_enabled,
    )

    all_kfs: list = []
    if episode_keyframes:
        for idx in sorted(episode_keyframes.keys()):
            all_kfs.extend(episode_keyframes[idx] or [])

    use_vision = bool(all_kfs) and vision_frames_enabled() and is_likely_vision_model()
    vision_cap = vision_effective_max_for_api() if use_vision else 0
    system_extra = ""
    if use_vision:
        n_vision_send = min(len(all_kfs), vision_cap)
        system_extra = (
            f" 已附 {n_vision_send} 张关键帧 JPEG：你必须结合亲眼看到的画面"
            "（打斗、表情、特效、构图）与对白/音效表写 clips，禁止只靠台词臆测。"
        )

    prompt_cap = _prompt_char_cap()
    compact_mode = os.getenv("HONGGUO_LLM_COMPACT_PROMPT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    prompt_for_user = (
        _compact_prompt_text(prompt, cap=prompt_cap) if compact_mode else prompt
    )
    sys_extra = human_impact_script_brief(body_sec=body_pick_sec) + _opening_prompt_block()
    if compact_mode:
        sys_extra = _compact_prompt_text(sys_extra, cap=max(500, prompt_cap // 2))

    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                MASTER_EDITOR_PERSONA
                + " 你只输出合法 JSON；产出对人最友好、最有冲击力的剪辑脚本（非剧情摘要）。"
                + sys_extra
                + clip_hint
                + system_extra
                + (" meme 方案须含 meme_captions、meme_beats。" if meme_edit_enabled() else "")
                + ai_compliance_rule_block()
            ),
        },
    ]

    if use_vision:
        messages.append(
            {
                "role": "user",
                "content": build_edit_vision_user_content(
                    episode_keyframes,
                    episode_labels,
                    task_prompt=prompt_for_user,
                    max_images=vision_cap,
                ),
            }
        )
    else:
        messages.append({"role": "user", "content": prompt_for_user})

    dur0 = episode_durations[0] if episode_durations else 120.0

    async def _complete(msgs: list[dict]) -> str:
        return await chat_completion(
            client, msgs, model=default_model(), json_mode=True
        )

    async def _complete_with_vision_fallback() -> str:
        """多模态 400 时先减帧再纯文本，避免误走 JSON 修复分支。"""
        try:
            return await _complete(messages)
        except RuntimeError as exc:
            if not use_vision or "400" not in str(exc):
                raise
            half = max(4, vision_cap // 2)
            if len(all_kfs) > half:
                logger.warning(
                    "多模态 LLM 400，减至 %d 张关键帧重试: %s",
                    half,
                    exc,
                )
                slim = [
                    messages[0],
                    {
                        "role": "user",
                        "content": build_edit_vision_user_content(
                            episode_keyframes,
                            episode_labels,
                            task_prompt=prompt_for_user,
                            max_images=half,
                        ),
                    },
                ]
                try:
                    return await _complete(slim)
                except RuntimeError:
                    pass
            logger.warning("多模态仍失败，改用对白+画面轴纯文本: %s", exc)
            return await _complete(
                [messages[0], {"role": "user", "content": prompt_for_user}]
            )

    raw_text = ""
    first_err: Optional[Exception] = None
    try:
        raw_text = await _complete_with_vision_fallback()
        raw = extract_json_object(raw_text)
        coerce_llm_edit_raw(
            raw,
            episode_count=len(episode_labels) or 1,
            body_target_sec=body_pick_sec,
            dur_avail=dur0,
        )
        sanitize_hook_only_plan_raw(
            raw, body_target_sec=body_pick_sec, dur_avail=dur0
        )
        repaired = repair_clip_durations_in_raw(
            raw, dur_avail=dur0, body_target_sec=body_pick_sec
        )
        if repaired:
            from multi_clip import clip_min_sec

            logger.info(
                "已自动将 %d 段过短 clip 抬至 ≥%.0fs（免 LLM 重答）",
                repaired,
                clip_min_sec(),
            )
        auto_fix_clip_gaps_in_raw(raw)
        val_errors = validate_ai_edit_raw(
            raw, body_target_sec=body_pick_sec, dur_avail=dur0
        )
        if val_errors and ai_edit_retry_enabled():
            logger.warning(
                "AI 分镜校验未通过（%d 项），自动重答一次：%s",
                len(val_errors),
                val_errors[0][:80],
            )
            retry_user = build_validation_retry_prompt(
                val_errors, raw_text, body_target_sec=body_pick_sec
            )
            raw_text = await _complete(
                messages
                + [
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content": retry_user},
                ]
            )
            raw = extract_json_object(raw_text)
            coerce_llm_edit_raw(
                raw,
                episode_count=len(episode_labels) or 1,
                body_target_sec=body_pick_sec,
                dur_avail=dur0,
            )
            sanitize_hook_only_plan_raw(
                raw, body_target_sec=body_pick_sec, dur_avail=dur0
            )
            repair_clip_durations_in_raw(
                raw, dur_avail=dur0, body_target_sec=body_pick_sec
            )
            auto_fix_clip_gaps_in_raw(raw)
            val2 = validate_ai_edit_raw(
                raw, body_target_sec=body_pick_sec, dur_avail=dur0
            )
            if val2:
                logger.warning("重答后仍有校验项：%s", "; ".join(val2[:3]))
        plan = _normalize_plan(
            raw,
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
            episode_transcripts=episode_transcripts,
            episode_edit_briefs=episode_edit_briefs,
        )
        apply_fixed_opening_to_plan(plan)
        parts = []
        if use_vision:
            parts.append(f"关键帧{len(all_kfs)}张")
        if has_visual:
            parts.append(f"画面轴{sum(len(v) for v in episode_visual_profiles.values())}段")
        if has_transcript:
            parts.append(f"对白{sum(len(v) for v in episode_transcripts.values())}条")
        tag = f"（{'+'.join(parts)}）" if parts else ""
        logger.info("AI 剪辑方案%s: %s", tag, plan.hook_summary or "ok")
        if transcript_edit_required() and not has_transcript:
            plan.hook_summary = (
                (plan.hook_summary or "") + "（警告：无对白时间轴，未按台词选段）"
            ).strip()
        return plan
    except Exception as exc:
        first_err = exc
        logger.warning("LLM 剪辑方案解析失败，尝试备用模型: %s", exc)

    try:
        fix_prompt = (
            "将以下内容修复为符合要求的 JSON 对象，只输出 JSON：\n"
            + (raw_text or str(first_err or "unknown"))
        )
        raw_text = await chat_completion(
            client,
            [
                {"role": "system", "content": "只输出 JSON。"},
                {"role": "user", "content": fix_prompt},
            ],
            model=code_model(),
            json_mode=True,
        )
        raw = extract_json_object(raw_text)
        coerce_llm_edit_raw(
            raw,
            episode_count=len(episode_labels) or 1,
            body_target_sec=body_pick_sec,
            dur_avail=dur0,
        )
        sanitize_hook_only_plan_raw(
            raw, body_target_sec=body_pick_sec, dur_avail=dur0
        )
        return _normalize_plan(
            raw,
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
            episode_transcripts=episode_transcripts,
            episode_edit_briefs=episode_edit_briefs,
        )
    except Exception as exc2:
        logger.warning("AI 剪辑方案失败，使用默认: %s", exc2)
        detail = format_api_error(exc2)
        if "401" in str(exc2) or "403" in str(exc2):
            fallback.hook_summary = f"LLM 鉴权失败：{detail[:200]}"
        else:
            fallback.hook_summary = f"AI 剪辑失败，已用默认方案：{detail[:120]}"
        return fallback
