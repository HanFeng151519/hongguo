"""大模型生成短剧推广成片剪辑方案，供 ffmpeg 执行。"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from qwen_client import (
    chat_completion,
    code_model,
    default_model,
    extract_json_object,
    format_api_error,
    is_configured,
)

logger = logging.getLogger(__name__)

MAX_PROMO_BODY_SEC = 120.0
MIN_PROMO_BODY_SEC = 15.0
DEFAULT_OPENING_SEC = 3.0
DEFAULT_OUTRO_SEC = 4.0
DEFAULT_BODY_PLAYBACK_SPEED = 2.0


def _body_playback_speed() -> float:
    raw = os.getenv("HONGGUO_BODY_PLAYBACK_SPEED", str(DEFAULT_BODY_PLAYBACK_SPEED))
    try:
        v = float(raw)
    except ValueError:
        v = DEFAULT_BODY_PLAYBACK_SPEED
    return max(0.5, min(4.0, v)) if v > 0 else DEFAULT_BODY_PLAYBACK_SPEED


@dataclass
class BodySegmentPlan:
    episode_index: int
    trim_start_sec: float = 0.0
    duration_sec: float = 60.0
    label: str = ""
    reason: str = ""


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

    def body_for_index(self, index: int) -> Optional[BodySegmentPlan]:
        for seg in self.body_segments:
            if seg.episode_index == index:
                return seg
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def plan_to_dict(plan: HookEditPlan) -> dict:
    return {
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
        f"别划走！《{short}》\n开头 30 秒就反转"
    )
    outro_keyword = (keyword or "").strip() or short
    segments: list[BodySegmentPlan] = []
    rng = random.Random(hash(f"{short}:{len(episode_labels)}"))
    for i, (label, dur) in enumerate(zip(episode_labels, episode_durations)):
        dur_f = float(dur or 120)
        trim = 8.0 if i == 0 and dur_f > 40 else 0.0
        trim = max(0.0, trim + rng.uniform(-2.0, 4.0))
        use = min(dur_f - trim, 75.0 if i == 0 else 55.0)
        segments.append(
            BodySegmentPlan(
                episode_index=i + 1,
                trim_start_sec=trim,
                duration_sec=_clamp(use, MIN_PROMO_BODY_SEC, MAX_PROMO_BODY_SEC),
                label=label,
                reason="默认：跳过铺垫，保留高潮段",
            )
        )
    post = (
        f"🔥《{short}》也太上头了！\n"
        f"第1集就高能，评论区说说你最气/最爽的是谁？\n"
        f"👉 红果搜「{short}」继续看\n"
        f"#{short} #短剧 #漫剧推荐"
    )
    commentary = [
        f"《{short}》高能预警",
        "这反转谁想得到？",
        f"红果搜「{short}」继续看",
    ]
    return HookEditPlan(
        opening_text=opening_text,
        opening_seconds=DEFAULT_OPENING_SEC,
        outro_keyword=outro_keyword,
        outro_seconds=DEFAULT_OUTRO_SEC,
        body_segments=segments,
        hook_summary="规则默认：短钩子片头 + 高潮正片裁剪",
        subtitle_hint=f"红果搜 {short}",
        commentary_lines=commentary,
        post_caption=post,
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
            speed = _body_playback_speed()
            max_source = max(0.0, dur_avail - trim_start)
            max_allowed = max(MIN_PROMO_BODY_SEC, max_source / speed)
            duration = _clamp(duration, MIN_PROMO_BODY_SEC, min(MAX_PROMO_BODY_SEC, max_allowed))
            label = str(item.get("label") or "")
            if not label and idx - 1 < len(episode_labels):
                label = episode_labels[idx - 1]
            segments.append(
                BodySegmentPlan(
                    episode_index=idx,
                    trim_start_sec=trim_start,
                    duration_sec=duration,
                    label=label,
                    reason=str(item.get("reason") or ""),
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

    return HookEditPlan(
        opening_text=opening_text,
        opening_seconds=opening_seconds,
        outro_keyword=outro_keyword,
        outro_seconds=outro_seconds,
        body_segments=segments,
        hook_summary=str(raw.get("hook_summary") or raw.get("summary") or ""),
        subtitle_hint=str(raw.get("subtitle_hint") or ""),
        post_caption=post_caption,
        commentary_lines=commentary_lines,
    )


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

    return f"""你是抖音/快手短剧投流编导，目标：高完播、高互动、引导搜索追剧，且成片需通过平台「原创/判重」（避免与同源推广素材高度相似）。
根据剧名与简介，自动设计横屏 16:9 正片裁剪方案，并写发布推文。

剧名：{drama_title}
简介：{drama_intro[:500] if drama_intro else "（无，请根据剧名推断类型：逆袭/修仙/霸总/虐恋等）"}
已选分集：
{eps_block}

请只输出 JSON（不要 markdown）：
{{
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
3. 成片正片默认 2 倍速：duration_sec 指成片时长；源片取用约为 duration_sec×2。
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
            "content": "你只输出合法 JSON 对象，字段名与用户要求一致。",
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
