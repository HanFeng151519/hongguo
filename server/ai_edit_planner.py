"""大模型生成短剧推广成片剪辑方案，供 ffmpeg 执行。"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Optional

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
from multi_clip import (
    ClipFragment,
    build_default_clips,
    clip_summary_for_log,
    clip_tail_pad_sec,
    clips_per_episode,
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

# 产品定位：AI = 剪辑大师，从整集里只留最精彩
MASTER_EDITOR_PERSONA = (
    "你是抖音短剧「钩子剪辑大师」：从每集完整正片里只挑最精彩的镜头，"
    "其余铺垫、闲聊、过场一律删掉。只留会让观众停下滑动的瞬间——"
    "冲突爆发、高能打脸、剧情反转、悬念断点。用快切拼成 3 秒抓眼的钩子。"
)
MASTER_EDITOR_REJECT = (
    "严禁选取：纯铺垫、走路、吃饭、重复镜头、无对白信息量的空镜、"
    "温和日常戏（除非内含反转伏笔且本段能看懂）。"
    "每段必须让一句对白说完再结束，禁止卡在人说话中间。"
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
    opening_text = (opening or "").strip() or (
        f"第1集就反转？《{short}》别划走"
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
    return HookEditPlan(
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


def _normalize_plan(
    raw: dict[str, Any],
    *,
    drama_title: str = "",
    opening: str = "",
    keyword: str = "",
    episode_labels: list[str],
    episode_durations: list[float],
) -> HookEditPlan:
    short = _title_short(drama_title)
    opening_text = (
        str(raw.get("opening_text") or opening).strip()
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
            trim_start = _clamp(
                float(item.get("trim_start_sec") or item.get("start_sec") or 0),
                0.0,
                max(0.0, dur_avail - MIN_PROMO_BODY_SEC),
            )
            duration = float(
                item.get("duration_sec") or item.get("duration") or 60
            )
            speed = body_playback_speed()
            max_source = max(0.0, dur_avail - trim_start)
            max_allowed = max(MIN_PROMO_BODY_SEC, max_source / speed)
            min_body = MIN_MEME_BODY_SEC if meme_edit_enabled() else MIN_PROMO_BODY_SEC
            max_body = MAX_MEME_BODY_SEC if meme_edit_enabled() else MAX_PROMO_BODY_SEC
            duration = _clamp(duration, min_body, min(max_body, max_allowed))
            label = str(item.get("label") or "")
            if not label and idx - 1 < len(episode_labels):
                label = episode_labels[idx - 1]
            raw_clips = normalize_clip_list(
                item.get("clips") or item.get("fragments") or item.get("cuts")
            )
            clip_rng = random.Random(f"{drama_title}:{idx}:{dur_avail:.0f}")
            if not raw_clips and multi_clip_enabled():
                raw_clips = build_default_clips(
                    dur_avail, duration, clip_rng, episode_index=idx
                )
            elif raw_clips and multi_clip_enabled():
                raw_clips = refine_clips_for_hook_arc(
                    raw_clips,
                    dur_avail=dur_avail,
                    target_sec=duration,
                    rng=clip_rng,
                    episode_index=idx,
                )
            trim, duration, raw_clips = sync_segment_from_clips(
                trim_start_sec=trim_start,
                duration_sec=duration,
                clips=raw_clips,
            )
            if raw_clips:
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
    if hook_budget_enabled(ep_n) and segments:
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

    return HookEditPlan(
        opening_text=opening_text,
        opening_seconds=opening_seconds,
        outro_keyword=outro_keyword,
        outro_seconds=outro_seconds,
        body_segments=segments,
        hook_summary=str(raw.get("hook_summary") or raw.get("summary") or ""),
        subtitle_hint=subtitle_hint,
        post_caption=post_caption,
        commentary_lines=commentary_lines,
        edit_style=edit_style,
        meme_captions=plan_caps,
        meme_beats=plan_beats,
    )


def _build_authentic_prompt(
    *,
    drama_title: str,
    drama_intro: str,
    episode_labels: list[str],
    episode_durations: list[float],
) -> str:
    eps_lines = []
    for i, (lab, dur) in enumerate(zip(episode_labels, episode_durations), start=1):
        eps_lines.append(f"  - 第{i}集「{lab}」可用时长约 {dur:.0f} 秒")
    eps_block = "\n".join(eps_lines) or "  - （无分集信息）"
    speed = body_playback_speed()
    n = len(episode_labels)
    per = per_episode_target_sec(n) if hook_budget_enabled(n) else 45.0
    if hook_budget_enabled(n):
        body = body_budget_seconds(n)
        per = per_episode_target_sec(n)
        body_lo = body_budget_seconds(n, total_sec=hook_target_min_sec())
        body_hi = body
        if multi_clip_enabled():
            duration_rule = (
                f"2. 共 {n} 集、正片合计约 {body_lo:.0f}–{body_hi:.0f}s（整条约 {hook_duration_range_text()}，"
                f"宁长勿短、优先剧情完整）；"
                f"每集 duration_sec≈{per:.0f}，clips 必须 {clips_per_episode()} 段快切，弧线：{hook_arc_hint_text()}。"
                f"每段 duration_sec 建议 9-13s（宁长勿短，禁止对白说到一半就切）；"
                f"trim_start_sec 须落在冲突/打脸/反转/悬念附近，"
                f"禁止平铺叙事或连续 30s；reason 必填且含「冲突/打脸/反转/悬念」之一；倍速 {speed:g}x。"
                f"系统会为每段自动留约 {clip_tail_pad_sec():.1f}s 尾音并做段间淡化。"
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
    return f"""{MASTER_EDITOR_PERSONA}
{MASTER_EDITOR_REJECT}
成片要求：正片保留原声对白（不叠解说条），用 clips 快切只留最精彩；系统会做去重与片头片尾。

剧名：{drama_title}
简介：{drama_intro[:500] if drama_intro else "（无）"}
已选分集：
{eps_block}

请只输出 JSON（不要 markdown）：
{{
  "edit_style": "authentic",
  "opening_text": "1 句短钩子，≤24 字，仅用于 1 秒解说卡配音",
  "outro_keyword": "搜索词 8-16 字",
  "post_caption": "发布文案 2-4 行 + 话题",
  "hook_summary": "一句话：本集删掉了什么、保留了哪几个最精彩瞬间",
  "commentary_lines": [],
  "body_segments": [
    {{
      "episode_index": 1,
      "duration_sec": {int(per) if hook_budget_enabled(n) else 45},
      "reason": "本集快切合集",
      "clips": [
        {{"trim_start_sec": 14, "duration_sec": 10, "reason": "起冲突：被羞辱"}},
        {{"trim_start_sec": 42, "duration_sec": 11, "reason": "高能打脸：碾压"}},
        {{"trim_start_sec": 68, "duration_sec": 12, "reason": "剧情反转：身份揭晓"}},
        {{"trim_start_sec": 95, "duration_sec": 10, "reason": "悬念断点：危机来临"}}
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
3. opening_text ≤ 24 字，必须含反转/高能暗示；hook_summary 说明本集选了哪些爽点。
4. 每集 clips 按时间顺序排列；第 1 集第一条尽量「开篇冲突」；最后一条尽量「悬念」。
5. body_segments 覆盖 episode_index 1 到 {n}。"""


def _build_prompt(
    *,
    drama_title: str,
    drama_intro: str,
    episode_labels: list[str],
    episode_durations: list[float],
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
2. 正片单集成片 30-75 秒；trim_start_sec 常 5-25 秒，保留爽点/反转。
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

    prompt = _build_prompt(
        drama_title=drama_title,
        drama_intro=drama_intro,
        episode_labels=episode_labels,
        episode_durations=episode_durations,
    )
    messages = [
        {
            "role": "system",
            "content": (
                MASTER_EDITOR_PERSONA
                + " 你只输出合法 JSON；像剪辑大师一样工作，每集只提交最精彩的片段时间点。"
                + (
                    " clips 弧线：冲突→打脸→反转→悬念；reason 必填高能类型；"
                    "每段 duration_sec 宁长勿短（9-13s），禁止对白说到一半就切。"
                    if multi_clip_enabled()
                    else " 删除铺垫，只留高潮与反转。"
                )
                + (" meme 方案须含 meme_captions、meme_beats。" if meme_edit_enabled() else "")
                + ai_compliance_rule_block()
            ),
        },
        {"role": "user", "content": prompt},
    ]

    raw_text = ""
    first_err: Optional[Exception] = None
    try:
        raw_text = await chat_completion(
            client, messages, model=default_model(), json_mode=True
        )
        raw = extract_json_object(raw_text)
        plan = _normalize_plan(
            raw,
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
        )
        logger.info("AI 剪辑方案: %s", plan.hook_summary or "ok")
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
        return _normalize_plan(
            raw,
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels,
            episode_durations=episode_durations,
        )
    except Exception as exc2:
        logger.warning("AI 剪辑方案失败，使用默认: %s", exc2)
        detail = format_api_error(exc2)
        if "401" in str(exc2) or "403" in str(exc2):
            fallback.hook_summary = f"LLM 鉴权失败：{detail[:200]}"
        else:
            fallback.hook_summary = f"AI 剪辑失败，已用默认方案：{detail[:120]}"
        return fallback
