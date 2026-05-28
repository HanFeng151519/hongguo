"""每集多片段快切：按「冲突→打脸→反转→悬念」弧线取高能片段。"""

from __future__ import annotations

import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# (reason, 时间比例, 权重, tier A|B)
_HOOK_ARC_4 = [
    ("A|起冲突：挑衅/羞辱/危机爆发", 0.12, 0.92, "A"),
    ("B|短衔接", 0.22, 0.45, "B"),
    ("A|高能打脸：碾压/放狠话/爽点", 0.36, 1.05, "A"),
    ("A|剧情反转：身份/误会/真相揭晓", 0.58, 1.18, "A"),
    ("B|悬念过渡", 0.72, 0.45, "B"),
    ("A|悬念断点：下一集必看", 0.84, 1.05, "A"),
]
_HOOK_ARC_3 = [
    ("A|开篇钩子：异象或冲突", 0.10, 0.95, "A"),
    ("A|高能+反转：打脸后反转", 0.48, 1.15, "A"),
    ("A|悬念卡断", 0.82, 1.05, "A"),
]
_HOOK_ARC_5 = [
    ("A|起冲突", 0.10, 0.9, "A"),
    ("B|衔接", 0.20, 0.45, "B"),
    ("A|高能打脸", 0.38, 1.05, "A"),
    ("A|神反转", 0.58, 1.18, "A"),
    ("A|悬念断点", 0.82, 1.05, "A"),
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


def ai_clip_strict_enabled() -> bool:
    """严格使用 AI 返回的 trim_start_sec / duration_sec，不做弧线重算、对白吸附、末帧定格补齐。"""
    raw = os.getenv("HONGGUO_AI_CLIP_STRICT", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return True


def body_pad_enabled() -> bool:
    """禁止用末帧定格硬凑正片时长（会造成几十秒假停顿）。"""
    v = os.getenv("HONGGUO_BODY_PAD_ENABLED", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def cohesive_edit_enabled() -> bool:
    """连贯少切：4~6 段、每段 7~12s，同场景尽量一镜到底。"""
    v = os.getenv("HONGGUO_CLIP_STYLE", "cohesive").strip().lower()
    if v in ("dense", "fast", "multi", "细切", "密集"):
        return False
    if v in ("cohesive", "连贯", "few", "less", "少切"):
        return True
    try:
        from hook_timeline import pro_60_template_enabled

        return pro_60_template_enabled()
    except ImportError:
        return True


def clips_per_episode(target_sec: float | None = None) -> int:
    """每集快切段数：连贯模式约 5 段；密集模式约 15 段。"""
    cohesive = cohesive_edit_enabled()
    auto = None
    if target_sec and target_sec >= 20.0:
        from hook_edit_methodology import clips_count_for_body

        auto = clips_count_for_body(target_sec, cohesive=cohesive)
    raw = os.getenv("HONGGUO_CLIPS_PER_EPISODE", "").strip()
    if raw:
        try:
            n = int(raw)
            if cohesive and n > 8 and auto is not None:
                return auto
            if cohesive:
                return max(4, min(6, n))
            if n <= 4 and target_sec and target_sec >= 30.0 and not cohesive:
                return max(8, min(16, int(round(float(target_sec) / 3.0))))
            return max(2, min(20, n))
        except ValueError:
            pass
    if auto is not None:
        return auto
    return 5 if cohesive else 4


def clip_min_sec(*, tier: str = "A") -> float:
    if tier == "B":
        try:
            from edge_tts_narration import dialogue_completeness_enabled

            if dialogue_completeness_enabled():
                return max(
                    4.0,
                    float(os.getenv("HONGGUO_CLIP_B_MIN_SEC", "5")),
                )
        except ImportError:
            pass
        return max(0.6, float(os.getenv("HONGGUO_CLIP_B_MIN_SEC", "0.8")))
    default = "6" if cohesive_edit_enabled() else "2"
    return max(1.5, float(os.getenv("HONGGUO_CLIP_MIN_SEC", default)))


def clip_max_sec(*, tier: str = "A") -> float:
    if tier == "B":
        try:
            from edge_tts_narration import dialogue_completeness_enabled

            b_hi = (
                "8"
                if dialogue_completeness_enabled()
                else os.getenv("HONGGUO_CLIP_B_MAX_SEC", "1.0")
            )
        except ImportError:
            b_hi = os.getenv("HONGGUO_CLIP_B_MAX_SEC", "1.0")
        return max(clip_min_sec(tier="B"), float(b_hi))
    default = "14" if cohesive_edit_enabled() else "4"
    return max(clip_min_sec(), float(os.getenv("HONGGUO_CLIP_MAX_SEC", default)))


def clip_tail_pad_sec() -> float:
    """略留尾音，保证金句说完；精剪模式不宜过长。"""
    return max(0.0, min(1.2, float(os.getenv("HONGGUO_CLIP_TAIL_PAD_SEC", "0.35"))))


def clip_crossfade_sec() -> float:
    """A/B 快切默认硬切（0）；需要时可开极短淡化。"""
    return max(0.0, min(0.8, float(os.getenv("HONGGUO_CLIP_CROSSFADE_SEC", "0"))))


def clip_min_gap_sec() -> float:
    """源片上相邻片段起点最小间隔，避免重复同一句话。"""
    default = "12" if cohesive_edit_enabled() else "3"
    return max(1.0, float(os.getenv("HONGGUO_CLIP_MIN_GAP_SEC", default)))


def hook_arc_hint_text() -> str:
    try:
        from hook_timeline import story_first_edit_enabled

        if story_first_edit_enabled():
            return (
                "故事完整优先：从对白表选齐六步叙事好情节；"
                "duration 覆盖句末；大跳剪加 B| 过渡；勿为控秒数删反转/尾钩"
            )
    except ImportError:
        pass
    if cohesive_edit_enabled():
        return (
            "连贯 4~6 段：开场≤11s、霸气≤16s、反转≤9s、尾钩≤7s；"
            "大跳剪到反转前须 1 段 B|过渡 5~8s；同场景少碎切；reason 标环节"
        )
    return (
        "A 类高光 2~4s/段 + 少量 B 类 ≤1s 衔接；按剧情顺序硬切；"
        "reason 以 A| 或 B| 开头（冲突/打脸/反转/过渡）"
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _reason_is_hooky(reason: str) -> bool:
    text = (reason or "").strip()
    if len(text) < 2:
        return False
    return any(k in text for k in _HOOKY_KEYWORDS)


def _beat_templates(n: int, episode_index: int) -> list[tuple[str, float, float, str]]:
    if cohesive_edit_enabled():
        pool = [b for b in _HOOK_ARC_5 if b[3] == "A"] or list(_HOOK_ARC_3)
        beats = []
        i = 0
        while len(beats) < n:
            beats.append(pool[i % len(pool)])
            i += 1
        beats = beats[:n]
    elif n <= 3:
        beats = list(_HOOK_ARC_3)
    else:
        pool = list(_HOOK_ARC_4) + list(_HOOK_ARC_5)
        beats = []
        i = 0
        while len(beats) < n:
            beats.append(pool[i % len(pool)])
            i += 1
        beats = beats[:n]
    if episode_index == 1 and beats:
        r, f, w, t = beats[0]
        beats[0] = ("A|开篇钩子：3秒抓眼冲突", f - 0.02, w, t)
    elif episode_index >= 3:
        beats = [(r, min(0.92, f + 0.04), w, t) for r, f, w, t in beats]
    return beats


def _allocate_durations(
    target_sec: float,
    n: int,
    weight_tpl: list[tuple[str, float, float, str]],
    rng: random.Random,
) -> list[float]:
    weights = [w * rng.uniform(0.92, 1.08) for _, _, w, _ in weight_tpl]
    wsum = sum(weights) or 1.0
    durs: list[float] = []
    for (_, _, w, tier), wt in zip(weight_tpl, weights):
        lo = clip_min_sec(tier=tier)
        hi = clip_max_sec(tier=tier)
        durs.append(_clamp(target_sec * wt / wsum, lo, hi))
    drift = sum(durs) - target_sec
    if abs(drift) > 0.5 and durs:
        # 超时长：先压 B 类，再压最长的 A
        order = sorted(
            range(len(durs)),
            key=lambda i: (0 if weight_tpl[i][3] == "B" else 1, -durs[i]),
        )
        for idx in order:
            if abs(drift) <= 0.5:
                break
            tier = weight_tpl[idx][3]
            lo = clip_min_sec(tier=tier)
            step = 0.35 if tier == "B" else 0.25
            if drift > 0:
                cut = min(drift, durs[idx] - lo)
                durs[idx] -= cut
                drift -= cut
            else:
                hi = clip_max_sec(tier=tier)
                add = min(-drift, hi - durs[idx])
                durs[idx] += add
                drift += add
    return durs


def normalize_clip_fragment(item: Any) -> ClipFragment | None:
    if not isinstance(item, dict):
        return None
    start = float(item.get("trim_start_sec") or item.get("start_sec") or 0)
    dur = float(item.get("duration_sec") or item.get("duration") or 0)
    end_raw = item.get("end_sec")
    if end_raw is not None:
        try:
            end_v = float(end_raw)
            if end_v > start + 0.35 and dur < 1.0:
                dur = end_v - start
        except (TypeError, ValueError):
            pass
    if dur < 0.45:
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


def enforce_hook_only_clips(
    clips: list[ClipFragment],
    *,
    target_sec: float,
    dur_avail: float,
) -> list[ClipFragment]:
    """钩子快切：限段数/单段时长/总时长，去重 reason（本地裁决）。"""
    if not clips:
        return clips
    try:
        from episode_edit_brief import (
            _ai_hook_only_enabled,
            _hook_clip_dict_score,
            _hook_plan_clip_bounds,
            _hook_plan_max_clips,
            _normalize_clip_dict_times,
            _reason_signature,
            _truncate_reason,
        )
    except ImportError:
        return clips
    if not _ai_hook_only_enabled():
        return clips
    try:
        from hook_timeline import story_first_edit_enabled

        if story_first_edit_enabled():
            return clips
    except ImportError:
        pass
    try:
        from hook_timeline import ai_body_script_max_sec

        cap_total = min(float(target_sec), ai_body_script_max_sec())
    except ImportError:
        cap_total = float(target_sec)

    lo, hi = _hook_plan_clip_bounds()
    max_clips = _hook_plan_max_clips()
    avail = max(lo + 1.0, float(dur_avail) - 0.5)
    out: list[ClipFragment] = []
    seen_pairs: list[tuple[str, float]] = []
    for c in clips:
        d: dict[str, Any] = {
            "trim_start_sec": c.trim_start_sec,
            "duration_sec": c.duration_sec,
            "reason": c.reason,
        }
        if not _normalize_clip_dict_times(d, dur_avail=avail):
            continue
        sig = _reason_signature(str(d.get("reason") or ""))
        trim = float(d["trim_start_sec"])
        if sig and any(abs(trim - t) < 4.0 and sig == s for s, t in seen_pairs):
            continue
        if any(abs(trim - t) < 1.0 for _, t in seen_pairs):
            continue
        dur = float(d["duration_sec"])
        if dur > hi:
            dur = hi
        elif dur < lo:
            dur = lo
        if sig:
            seen_pairs.append((sig, trim))
        out.append(
            ClipFragment(
                trim_start_sec=trim,
                duration_sec=dur,
                reason=_truncate_reason(str(d.get("reason") or "")),
            )
        )

    if len(out) > max_clips:
        ranked = sorted(
            out,
            key=lambda x: (
                -_hook_clip_dict_score(
                    {
                        "duration_sec": x.duration_sec,
                        "reason": x.reason,
                    }
                ),
                x.trim_start_sec,
            ),
        )
        out = sorted(ranked[:max_clips], key=lambda x: x.trim_start_sec)

    total = sum(c.duration_sec for c in out)
    if total < cap_total * 0.88 and out:
        ratio = min(1.45, cap_total / max(total, 0.01))
        for c in out:
            c.duration_sec = _clamp(c.duration_sec * ratio, lo, hi)
        total = sum(c.duration_sec for c in out)
    if total > cap_total + 0.35 and out:
        ratio = cap_total / max(total, 0.01)
        for c in out:
            c.duration_sec = _clamp(c.duration_sec * ratio, lo, hi)
        total = sum(c.duration_sec for c in out)
        while total > cap_total + 0.35 and len(out) > 3:
            drop_i = min(
                range(len(out)),
                key=lambda i: _hook_clip_dict_score(
                    {"duration_sec": out[i].duration_sec, "reason": out[i].reason}
                ),
            )
            out.pop(drop_i)
            total = sum(c.duration_sec for c in out)

    if out:
        logger.info(
            "钩子 clips 本地裁决：%d 段合计 %.1fs（≤%.0fs）",
            len(out),
            total,
            cap_total,
        )
    return out


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
    for i, ((reason_tpl, frac, _, _tier), dur) in enumerate(zip(templates, durs)):
        orig = clips[i] if i < len(clips) else clips[-1]
        jitter = rng.uniform(-0.035, 0.035)
        anchor = _clamp((avail - dur - 2.0) * (frac + jitter), 0.0, avail - dur - 1.0)
        if _reason_is_hooky(orig.reason) and orig.trim_start_sec > 0:
            start = 0.45 * anchor + 0.55 * orig.trim_start_sec
            reason = orig.reason if orig.reason.startswith(("A|", "B|")) else f"A|{orig.reason}"
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


def ensure_clip_spacing(
    clips: list[ClipFragment],
    *,
    strict: bool = False,
) -> list[ClipFragment]:
    """相邻片段拉开间距；strict 时仅消除重叠，不强行拉开 12s 间隔。"""
    from hook_edit_methodology import clip_tier

    if not clips:
        return []
    gap = 2.0 if strict else clip_min_gap_sec()
    out: list[ClipFragment] = []
    for i, c in enumerate(clips):
        if clip_tier(c.reason) == "C":
            continue
        start = c.trim_start_sec
        tier = clip_tier(c.reason)
        min_d = clip_min_sec(tier=tier)
        max_d = clip_max_sec(tier=tier)
        if out:
            prev = out[-1]
            prev_end = prev.trim_start_sec + prev.duration_sec
            if start < prev_end + (0.35 if strict else gap):
                start = prev_end + (0.35 if strict else gap)
        max_d = (min(20.0, max_d * 1.6) if strict else max_d)
        dur = _clamp(c.duration_sec, min_d, max_d)
        out.append(
            ClipFragment(trim_start_sec=start, duration_sec=dur, reason=c.reason)
        )
    return out


def apply_ai_clips_strict(
    clips: list[ClipFragment],
    *,
    dur_avail: float,
    target_sec: float,
    preserve_duration: bool = False,
) -> list[ClipFragment]:
    """保留 AI 入点与相对时长，按正片预算等比拉伸，禁止末帧定格凑时长。

    preserve_duration=True：只做入点/片长边界与最短时长，不压缩或等比缩到 target（完整对白版用）。
    """
    if not clips:
        return clips
    avail = max(10.0, float(dur_avail or 120) - 3.0)
    cleaned: list[ClipFragment] = []
    for c in sorted(clips, key=lambda x: x.trim_start_sec):
        text = (c.reason or "").strip()
        if not text:
            continue
        start = max(0.0, min(avail - 1.5, float(c.trim_start_sec)))
        dur = max(clip_min_sec(), float(c.duration_sec))
        end = start + dur
        if end > avail:
            dur = max(clip_min_sec(tier="B"), avail - start)
            start = max(0.0, min(start, avail - dur))
        cleaned.append(ClipFragment(trim_start_sec=start, duration_sec=dur, reason=text))

    if not cleaned:
        return clips

    overhead = clip_export_overhead_sec(len(cleaned))
    budget = max(8.0, float(target_sec) - overhead)
    total = sum(c.duration_sec for c in cleaned)
    if total < 0.5:
        return ensure_clip_spacing(cleaned, strict=True)
    if preserve_duration:
        out = enforce_clip_duration_floor(_dedupe_and_sort_clips(cleaned), avail)
        logger.info(
            "严格 AI 分镜（完整对白）%d 段 → 计划正片 %.1fs（不压缩至 %.0fs）",
            len(out),
            sum(c.duration_sec for c in out) + overhead,
            target_sec,
        )
        return out
    from edge_tts_narration import dialogue_completeness_enabled

    if abs(total - budget) > 0.8:
        if dialogue_completeness_enabled() and total > budget:
            from edge_tts_narration import (
                dialogue_compress_enabled,
                should_compress_for_dialogue,
            )

            if dialogue_compress_enabled() and should_compress_for_dialogue(
                total, budget
            ):
                cleaned = compress_clips_abc_priority(cleaned, budget)
        elif dialogue_completeness_enabled() and total < budget:
            try:
                from hook_timeline import story_first_edit_enabled

                if story_first_edit_enabled():
                    pass
                else:
                    ratio = min(1.25, budget / total)
                    for c in cleaned:
                        c.duration_sec = _clamp(
                            c.duration_sec * ratio, c.duration_sec, clip_max_sec()
                        )
                    logger.info(
                        "严格 AI 分镜：正片 %.1fs 低于预算 %.1fs，已按比例补时长",
                        total,
                        budget,
                    )
            except ImportError:
                pass
        else:
            ratio = budget / total
            floor = 1.8 if dialogue_completeness_enabled() else 1.5
            for c in cleaned:
                scaled = c.duration_sec * ratio
                if dialogue_completeness_enabled():
                    scaled = max(scaled, c.duration_sec * 0.9)
                c.duration_sec = _clamp(scaled, floor, 20.0)

    out = enforce_clip_duration_floor(_dedupe_and_sort_clips(cleaned), avail)
    logger.info(
        "严格 AI 分镜 %d 段 → 计划正片 %.1fs（入点 %.0f/%.0f/…，最短≥%.0fs）",
        len(out),
        sum(c.duration_sec for c in out) + overhead,
        out[0].trim_start_sec if out else 0,
        out[1].trim_start_sec if len(out) > 1 else 0,
        clip_min_sec(),
    )
    return out


def _clip_is_b_tier(reason: str) -> bool:
    r = (reason or "").strip()
    return r.startswith("B|") or r.startswith("B｜")


def enforce_clip_duration_floor(
    clips: list[ClipFragment],
    dur_avail: float,
    *,
    cues: list | None = None,
) -> list[ClipFragment]:
    """禁止超短碎切（如 1.4s）；B| 在对白完整模式下也须≥5s。"""
    if not clips:
        return clips
    lo_a = clip_min_sec()
    lo_b = clip_min_sec(tier="B")
    avail = max(lo_a + 1.0, float(dur_avail) - 0.5)
    out: list[ClipFragment] = []
    for c in clips:
        dur = float(c.duration_sec)
        start = max(0.0, float(c.trim_start_sec))
        lo = lo_b if _clip_is_b_tier(c.reason or "") else lo_a
        if dur >= lo - 0.05:
            out.append(c)
            continue
        if cues:
            from video_transcript import find_cue_at

            anchor = find_cue_at(cues, start)
            if anchor:
                from edge_tts_narration import dialogue_tail_pad_sec

                pad = dialogue_tail_pad_sec()
                start = max(0.0, anchor.start_sec - 0.35)
                end = anchor.end_sec + pad
                for cue in cues:
                    if cue.start_sec > anchor.end_sec + 20.0:
                        break
                    if anchor.start_sec - 0.1 <= cue.start_sec <= anchor.end_sec + 12.0:
                        end = max(end, cue.end_sec + pad)
                dur = max(lo, end - start)
        else:
            dur = lo
        if start + dur > avail:
            dur = max(lo, avail - start)
        logger.info(
            "clip 过短已抬至 %.1fs（入点 %.1fs，原 %.1fs）",
            dur,
            start,
            c.duration_sec,
        )
        out.append(
            ClipFragment(
                trim_start_sec=start,
                duration_sec=round(dur, 2),
                reason=c.reason,
            )
        )
    return out


def compress_clips_abc_priority(
    clips: list[ClipFragment],
    target_sec: float,
) -> list[ClipFragment]:
    """超时长：先缩短 B 类，再缩短偏长的 A 类（保持剧情顺序）。"""
    from hook_edit_methodology import clip_tier

    if not clips or target_sec <= 0:
        return clips
    overhead = clip_export_overhead_sec(len(clips))
    budget = max(clip_min_sec() * len(clips), target_sec - overhead)
    clips = [c for c in clips if clip_tier(c.reason) != "C"]
    if not clips:
        return clips

    def _total() -> float:
        return sum(c.duration_sec for c in clips)

    guard = 0
    while _total() > budget + 0.4 and guard < 80:
        guard += 1
        b_idx = [
            i
            for i, c in enumerate(clips)
            if clip_tier(c.reason) == "B"
            and c.duration_sec > clip_min_sec(tier="B") + 0.05
        ]
        if b_idx:
            i = max(b_idx, key=lambda j: clips[j].duration_sec)
            clips[i].duration_sec = max(
                clip_min_sec(tier="B"),
                clips[i].duration_sec - 0.25,
            )
            continue
        a_idx = [
            i
            for i, c in enumerate(clips)
            if clip_tier(c.reason) == "A"
            and c.duration_sec > clip_min_sec() + 0.1
        ]
        if not a_idx:
            break
        i = max(a_idx, key=lambda j: clips[j].duration_sec)
        clips[i].duration_sec = max(clip_min_sec(), clips[i].duration_sec - 0.2)
    return clips


def ensure_clips_count(
    clips: list[ClipFragment],
    *,
    dur_avail: float,
    target_sec: float,
    rng: random.Random,
    episode_index: int = 1,
) -> list[ClipFragment]:
    """AI 段数过少时补全；连贯模式不强行补到 15 段。"""
    want = clips_per_episode(target_sec)
    if cohesive_edit_enabled():
        if not clips:
            return build_default_clips(
                dur_avail, target_sec, rng, episode_index=episode_index
            )
        if len(clips) >= max(4, want - 1):
            return clips[:want]
    if not clips or len(clips) >= want:
        return clips[:want] if clips else clips
    generated = build_default_clips(
        dur_avail, target_sec, rng, episode_index=episode_index
    )
    merged = sorted(clips, key=lambda c: c.trim_start_sec)
    used_starts = {round(c.trim_start_sec, 1) for c in merged}
    for g in generated:
        if len(merged) >= want:
            break
        key = round(g.trim_start_sec, 1)
        if key in used_starts:
            continue
        merged.append(g)
        used_starts.add(key)
    merged = ensure_clip_spacing(_dedupe_and_sort_clips(sorted(merged, key=lambda c: c.trim_start_sec)))
    if len(merged) < want:
        for g in generated:
            if len(merged) >= want:
                break
            key = round(g.trim_start_sec, 1)
            if key not in used_starts:
                merged.append(g)
                used_starts.add(key)
        merged = ensure_clip_spacing(_dedupe_and_sort_clips(sorted(merged, key=lambda c: c.trim_start_sec)))
    return merged[:want]


def build_default_clips(
    dur_avail: float,
    target_sec: float,
    rng: random.Random,
    *,
    episode_index: int = 1,
) -> list[ClipFragment]:
    """无 AI 时按 A/B 高能弧线生成快切点。"""
    n = clips_per_episode(target_sec)
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
    from hook_edit_methodology import clip_tier

    if not clips or target_sec <= 0:
        return
    overhead = clip_export_overhead_sec(len(clips))
    target_sec = max(clip_min_sec() * len(clips), target_sec - overhead)
    if not cohesive_edit_enabled():
        clips[:] = compress_clips_abc_priority(clips, target_sec)
    total = sum(c.duration_sec for c in clips)
    if total < 0.01:
        per = target_sec / len(clips)
        for c in clips:
            tier = clip_tier(c.reason)
            c.duration_sec = _clamp(
                per, clip_min_sec(tier=tier), clip_max_sec(tier=tier)
            )
        return
    if abs(total - target_sec) <= 0.8:
        return
    ratio = target_sec / total
    for c in clips:
        tier = clip_tier(c.reason)
        lo, hi = clip_min_sec(tier=tier), clip_max_sec(tier=tier)
        c.duration_sec = _clamp(c.duration_sec * ratio, lo, hi)
    total2 = sum(c.duration_sec for c in clips)
    if total2 < target_sec * 0.92:
        ratio2 = target_sec / max(total2, 0.01)
        for c in clips:
            tier = clip_tier(c.reason)
            lo, hi = clip_min_sec(tier=tier), clip_max_sec(tier=tier)
            c.duration_sec = _clamp(c.duration_sec * ratio2, lo, hi)
    elif not cohesive_edit_enabled():
        clips[:] = compress_clips_abc_priority(clips, target_sec)


def clip_summary_for_log(clips: list[ClipFragment]) -> str:
    return " | ".join(
        f"{c.reason or '片段'}@{c.trim_start_sec:.0f}s/{c.duration_sec:.0f}s"
        for c in clips[:6]
    )
