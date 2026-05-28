"""分集剪辑简报：结构分析、范例分镜、AI 方案校验与重试。"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 全品类短剧通用相位（命中本集对白才写入结构地图，非固定模板）
_PHASE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("冲突对峙", ("凭什么", "你敢", "不配", "滚", "放肆", "可恶", "别逼我")),
    ("情绪爆发", ("对不起", "骗子", "误会", "后悔", "别走", "不能", "凭什么")),
    ("反转/真相", ("原来", "真相", "没想到", "竟然", "秘密", "是谁", "假的")),
    ("关系/身份", ("离婚", "结婚", "总裁", "老板", "小姐", "夫人", "身份")),
    ("悬念/尾钩", ("下一集", "等着", "没完", "走着瞧", "你等着")),
]

_TOPIC_STOPWORDS = frozenset({
    "高能",
    "画面",
    "对白",
    "开场",
    "跳剪",
    "跳切",
    "视听",
    "钩子",
    "前段",
    "后段",
    "中段",
    "尾钩",
    "悬念",
    "冲突",
    "转折",
    "tags",
    "motion",
    "fight",
    "sfx",
    "high",
    "start",
    "end",
    "sec",
})

_SEC_RE = re.compile(
    r"start_sec\s*=\s*([0-9.]+)|end_sec\s*=\s*([0-9.]+)|"
    r"([0-9.]+)\s*-\s*([0-9.]+)"
)


def ai_edit_retry_enabled() -> bool:
    v = os.getenv("HONGGUO_AI_EDIT_RETRY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def max_clip_gap_sec() -> float:
    raw = os.getenv("HONGGUO_MAX_CLIP_GAP_SEC", "").strip()
    if raw:
        try:
            return max(12.0, float(raw))
        except ValueError:
            pass
    # 连贯 5 段钩子常需跳过中段回忆，默认略放宽（原 22s 易误伤 26s 合理跳剪）
    try:
        from multi_clip import cohesive_edit_enabled

        if cohesive_edit_enabled():
            return 35.0
    except ImportError:
        pass
    return 30.0


def _reason_allows_source_gap(reason: str) -> bool:
    """大间隔跳剪须在 reason 标明，避免观众以为漏剧情。"""
    r = (reason or "").strip()
    if not r:
        return False
    markers = (
        "跳剪",
        "跳切",
        "跳镜",
        "大跳",
        "省略",
        "跳过",
        "略过",
    )
    if any(m in r for m in markers):
        return True
    return r.startswith("B|") or r.startswith("B｜")


def clip_gap_strict_validation() -> bool:
    """为 1 时大间隔未标跳剪会触发校验失败/LLM 重答；默认自动补标并继续。"""
    v = os.getenv("HONGGUO_CLIP_GAP_STRICT", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def auto_fix_clip_gaps_in_raw(raw: dict[str, Any]) -> int:
    """大间隔跳剪自动在 reason 补「跳剪」，避免无意义重答。"""
    max_gap = max_clip_gap_sec()
    fixes = 0
    for seg in raw.get("body_segments") or []:
        if not isinstance(seg, dict):
            continue
        clips = seg.get("clips") or []
        if not isinstance(clips, list):
            continue
        sorted_clips = sorted(
            (c for c in clips if isinstance(c, dict)),
            key=lambda x: float(x.get("trim_start_sec") or 0),
        )
        prev_end = 0.0
        for i, clip in enumerate(sorted_clips):
            trim = float(clip.get("trim_start_sec") or 0)
            dur = float(clip.get("duration_sec") or 0)
            reason = str(clip.get("reason") or "")
            gap = trim - prev_end
            if i > 0 and gap > max_gap and not _reason_allows_source_gap(reason):
                clip["reason"] = f"{reason}（跳剪）".strip()
                fixes += 1
            prev_end = trim + dur
    if fixes:
        logger.info("已自动为 %d 处大间隔 clip 补标「跳剪」（免 LLM 重答）", fixes)
    return fixes


def duration_tolerance_sec() -> float:
    try:
        return max(0.3, float(os.getenv("HONGGUO_CLIP_DURATION_TOL", "0.5")))
    except ValueError:
        return 0.5


def front_heavy_edit_enabled() -> bool:
    """略偏前段爽点，后段仍保留冲突/铺垫/悬念（非一刀切删后段）。"""
    v = os.getenv("HONGGUO_HOOK_FRONT_HEAVY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def late_start_ratio() -> float:
    """入点超过片长该比例视为「后段」；第1集约 161s、0.58 时 ≈93s 以后。"""
    try:
        return min(0.75, max(0.35, float(os.getenv("HONGGUO_LATE_START_RATIO", "0.58"))))
    except ValueError:
        return 0.58


def max_late_body_sec() -> float:
    """后段 clips 时长合计上限（冲突铺垫/轻虐/集末悬念）。"""
    try:
        return max(6.0, float(os.getenv("HONGGUO_MAX_LATE_BODY_SEC", "18")))
    except ValueError:
        return 18.0


def min_late_body_sec() -> float:
    """后段至少保留的时长，避免铺垫被压没。"""
    try:
        return max(0.0, float(os.getenv("HONGGUO_MIN_LATE_BODY_SEC", "10")))
    except ValueError:
        return 10.0


def opening_anchor_max_sec() -> float:
    """首段入点超过该秒数则视为「未做片头钩子」，触发锚定或校验重答。"""
    try:
        return max(8.0, float(os.getenv("HONGGUO_OPENING_ANCHOR_MAX_SEC", "18")))
    except ValueError:
        return 18.0


def opening_clip_min_sec() -> float:
    try:
        return max(8.0, float(os.getenv("HONGGUO_OPENING_CLIP_MIN_SEC", "10")))
    except ValueError:
        return 10.0


def narrative_cohesion_enabled() -> bool:
    """规则引擎强约束（与 AI 自主创作互斥，默认关）。"""
    try:
        from hook_edit_methodology import ai_editor_autonomy_enabled

        if ai_editor_autonomy_enabled():
            return False
    except ImportError:
        pass
    v = os.getenv("HONGGUO_NARRATIVE_COHESION", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def opening_first_clip_max_sec() -> float:
    try:
        return max(5.0, min(12.0, float(os.getenv("HONGGUO_OPENING_FIRST_MAX_SEC", "8"))))
    except ValueError:
        return 8.0


def max_cohesive_clip_count() -> int:
    try:
        return max(3, min(6, int(os.getenv("HONGGUO_MAX_COHESIVE_CLIPS", "4"))))
    except ValueError:
        return 4


def max_late_scatter_clips() -> int:
    try:
        return max(1, min(3, int(os.getenv("HONGGUO_MAX_LATE_SCATTER_CLIPS", "2"))))
    except ValueError:
        return 2


_TRANSITION_MARKERS = (
    "穿梭",
    "走位",
    "走路",
    "换场",
    "切换",
    "过场",
    "跟拍",
    "人物移动",
    "无对白",
    "空镜",
    "远景",
    "铺垫",
)


def _is_narrative_arc_clip(reason: str) -> bool:
    """六步叙事环节，禁止当无信息过场删掉。"""
    r = (reason or "").strip()
    return any(
        k in r
        for k in (
            "开场高能",
            "霸气",
            "立势",
            "立人设",
            "反转",
            "尾钩",
            "引流",
            "反转铺垫",
            "情感",
            "回忆",
            "收徒",
            "伏笔",
            "过渡",
            "父母",
            "房间",
        )
    )


def _is_transition_shot(reason: str) -> bool:
    from hook_edit_methodology import clip_tier

    r = (reason or "").strip()
    if _is_narrative_arc_clip(r):
        return False
    if clip_tier(r) == "C":
        return True
    if any(m in r for m in _TRANSITION_MARKERS):
        return True
    if r.startswith("B|") and not any(
        k in r
        for k in (
            "对白",
            "台词",
            "羞辱",
            "打脸",
            "冲突",
            "反转",
            "悬念",
            "过渡",
            "情感",
            "回忆",
            "铺垫",
            "收徒",
            "伏笔",
            "承上",
            "衔接",
        )
    ):
        return True
    return False


def _first_dialogue_anchor(cues: list) -> Optional[tuple[float, float, str]]:
    """(start, end, text) 第一句有文本对白。"""
    for c in cues or []:
        text = _cue_text(c).strip()
        if len(text) < 2:
            continue
        st = _cue_start(c)
        en = _cue_end(c)
        if en > st:
            return st, en, text[:24]
    return None


def arc_pacing_polish_enabled() -> bool:
    v = os.getenv("HONGGUO_ARC_PACING_POLISH", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _arc_phase(reason: str) -> str:
    r = (reason or "").strip()
    if "开场" in r or "视听钩子" in r:
        return "open"
    if "霸气" in r or "立势" in r or "立人设" in r:
        return "power"
    if "反转" in r:
        return "twist"
    if "尾钩" in r or "引流" in r or ("悬念" in r and "尾" in r):
        return "hook"
    if any(k in r for k in ("情感", "回忆", "伏笔", "父母")):
        return "hook"
    return "other"


def body_span_bounds() -> tuple[float, float]:
    """故事完整模式下正片目标区间（秒）。"""
    try:
        lo = max(40.0, float(os.getenv("HONGGUO_BODY_SPAN_MIN", "55")))
        hi = max(lo + 3.0, float(os.getenv("HONGGUO_BODY_SPAN_MAX", "70")))
    except ValueError:
        lo, hi = 55.0, 70.0
    return lo, hi


_TRIM_PHASE_PRIORITY = {
    "power": 0,
    "other": 1,
    "open": 2,
    "twist": 3,
    "hook": 4,
}


def _cap_clips_body_span(
    clips: list,
    *,
    dur_avail: float,
) -> list:
    """正片总长超过上限时，优先从霸气/过渡段尾部收短，保留反转与尾钩。"""
    if not clips:
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    lo_bound, hi_bound = body_span_bounds()
    lo = clip_min_sec()
    total = sum(float(c.duration_sec) for c in clips)
    if total <= hi_bound + 0.05:
        if total < lo_bound - 0.5:
            logger.info(
                "正片 %.1fs 短于目标下限 %.0fs（仅按 reason end_sec，不补扫对白）",
                total,
                lo_bound,
            )
        return clips

    out: list[ClipFragment] = list(clips)
    excess = total - hi_bound
    ranked = sorted(
        range(len(out)),
        key=lambda i: (
            _TRIM_PHASE_PRIORITY.get(_arc_phase(out[i].reason or ""), 1),
            -float(out[i].duration_sec),
        ),
    )
    trimmed = 0
    for i in ranked:
        if excess <= 0.05:
            break
        c = out[i]
        d = float(c.duration_sec)
        if d <= lo + 0.05:
            continue
        cut = min(excess, d - lo)
        if cut <= 0.05:
            continue
        out[i] = ClipFragment(
            trim_start_sec=c.trim_start_sec,
            duration_sec=round(d - cut, 2),
            reason=c.reason,
        )
        excess -= cut
        trimmed += 1

    new_total = sum(c.duration_sec for c in out)
    logger.info(
        "正片时长 cap：%.1fs → %.1fs（目标 %d–%ds，%d 段收短）",
        total,
        new_total,
        int(lo_bound),
        int(hi_bound),
        trimmed,
    )
    return out


def extend_clips_to_reason_span(
    clips: list,
    cues: list,
    *,
    dur_avail: float,
) -> list:
    """仅按 reason 的 start_sec/end_sec 校正入点与出点；不扫对白表吞句。总长超 70s 则收短。"""
    if not clips:
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    try:
        from edge_tts_narration import (
            dialogue_completeness_enabled,
            dialogue_tail_pad_sec,
        )

        if not dialogue_completeness_enabled():
            return clips
        pad = dialogue_tail_pad_sec()
    except ImportError:
        pad = 0.45

    lo = clip_min_sec()
    avail = max(lo + 1.0, float(dur_avail) - 0.5)
    out: list[ClipFragment] = []
    changed = 0

    for c in sorted(clips, key=lambda x: x.trim_start_sec):
        reason = c.reason or ""
        st_r, en_r = _parse_times_from_reason(reason)
        trim = float(c.trim_start_sec)
        dur = float(c.duration_sec)

        if st_r is not None and abs(trim - st_r) < 10.0:
            trim = max(0.0, st_r - 0.25)

        if en_r is not None and en_r > trim:
            reason_max = en_r - trim + pad
            if reason_max < lo:
                new_dur = max(0.5, reason_max)
            elif dur < reason_max - 0.35:
                new_dur = max(lo, reason_max)
            else:
                new_dur = max(lo, min(dur, reason_max))
        else:
            new_dur = max(lo, dur)

        if trim + new_dur > avail:
            new_dur = max(lo, avail - trim)

        if abs(trim - float(c.trim_start_sec)) > 0.05 or new_dur > dur + 0.35:
            changed += 1
        out.append(
            ClipFragment(
                trim_start_sec=round(trim, 2),
                duration_sec=round(new_dur, 2),
                reason=reason,
            )
        )

    out = _cap_clips_body_span(out, dur_avail=avail)
    if changed:
        logger.info(
            "对白跨度校正：%d/%d 段已按 reason end_sec 对齐 → 正片 %.1fs",
            changed,
            len(out),
            sum(x.duration_sec for x in out),
        )
    return out


def _arc_cap_sec(phase: str) -> float:
    from multi_clip import clip_max_sec

    hi = clip_max_sec()
    defaults = {
        "open": ("HONGGUO_ARC_OPEN_MAX_SEC", min(11.0, hi)),
        "power": ("HONGGUO_ARC_POWER_MAX_SEC", min(16.0, hi)),
        "twist": ("HONGGUO_ARC_TWIST_MAX_SEC", min(9.0, hi)),
        "hook": ("HONGGUO_ARC_HOOK_MAX_SEC", min(7.0, hi)),
    }
    key, default = defaults.get(phase, ("", 14.0))
    if not key:
        return default
    try:
        return max(4.0, float(os.getenv(key, str(default))))
    except ValueError:
        return default


def _bridge_gap_sec() -> float:
    try:
        return max(12.0, float(os.getenv("HONGGUO_ARC_BRIDGE_GAP_SEC", "18")))
    except ValueError:
        return 18.0


def _find_cue_in_source_range(cues: list, lo: float, hi: float):
    if hi <= lo + 1.0:
        return None
    pool = [
        c
        for c in cues or []
        if lo + 0.5 <= _cue_start(c) < hi - 0.5 and len(_cue_text(c).strip()) >= 3
    ]
    if not pool:
        return None
    mid = (lo + hi) * 0.5
    return min(pool, key=lambda c: abs(_cue_start(c) - mid))


def _dialogue_span_cap(c: Any, cues: list, dur_avail: float, lo: float) -> float:
    """该 clip 在源片上可安全延长到的 duration 上限（对白句末）。"""
    st, en = _parse_times_from_reason(c.reason or "")
    if st is not None and en is not None and en > st:
        return max(lo, min(en - st + 0.55, dur_avail - c.trim_start_sec))
    for cue in cues or []:
        cs, ce = _cue_start(cue), _cue_end(cue)
        if cs >= c.trim_start_sec - 0.5 and ce <= c.trim_start_sec + c.duration_sec + 8.0:
            return max(lo, min(ce - c.trim_start_sec + 0.55, dur_avail - c.trim_start_sec))
    return max(lo, min(float(c.duration_sec) + 4.0, dur_avail - c.trim_start_sec))


def restore_arc_body_total(
    clips: list,
    *,
    target_sec: float,
    cues: list,
    dur_avail: float,
) -> list:
    """节奏修剪后把正片总时长拉回目标，避免故事被压短（完整度优先）。"""
    if not clips or target_sec <= 0:
        return clips
    from multi_clip import ClipFragment, clip_max_sec, clip_min_sec

    lo = clip_min_sec()
    hi = clip_max_sec()
    try:
        from edge_tts_narration import dialogue_completeness_enabled

        if not dialogue_completeness_enabled():
            return clips
    except ImportError:
        return clips

    out = list(clips)
    total = sum(c.duration_sec for c in out)
    floor = max(42.0, float(target_sec) * 0.88)
    if total >= floor - 0.5:
        return out

    need = floor - total
    priority = {"twist": 0, "hook": 1, "power": 2, "open": 3, "other": 4}

    def _prio(c) -> tuple[int, float]:
        ph = _arc_phase(c.reason or "")
        return priority.get(ph, 4), -float(c.trim_start_sec)

    extendable: list[tuple[int, float]] = []
    for i, c in enumerate(out):
        room = min(hi, _dialogue_span_cap(c, cues, dur_avail, lo)) - c.duration_sec
        if room > 0.35:
            extendable.append((i, room))
    extendable.sort(key=lambda x: _prio(out[x[0]]))

    for i, room in extendable:
        if need <= 0.2:
            break
        add = min(need, room, hi - out[i].duration_sec)
        if add < 0.25:
            continue
        c = out[i]
        out[i] = ClipFragment(
            trim_start_sec=c.trim_start_sec,
            duration_sec=round(c.duration_sec + add, 2),
            reason=c.reason,
        )
        need -= add

    new_total = sum(c.duration_sec for c in out)
    if new_total > total + 0.5:
        logger.info(
            "叙事完整：正片由 %.1fs 补回 %.1fs（目标≥%.0fs）",
            total,
            new_total,
            floor,
        )
    return out


def polish_arc_pacing_clips(
    clips: list,
    cues: list,
    *,
    dur_avail: float,
    target_sec: float = 0.0,
) -> list:
    """单段过长则拆分/限长，但不砍总时长；反转前可补 B 过渡。"""
    if not clips or not arc_pacing_polish_enabled():
        return clips
    try:
        from hook_timeline import story_first_edit_enabled

        story_first = story_first_edit_enabled()
    except ImportError:
        story_first = False
    from multi_clip import ClipFragment, clip_min_sec

    lo = clip_min_sec()
    sorted_c = sorted(clips, key=lambda c: c.trim_start_sec)
    notes: list[str] = []

    capped: list[ClipFragment] = list(sorted_c)
    if not story_first:
        capped = []
        for c in sorted_c:
            phase = _arc_phase(c.reason or "")
            cap = _arc_cap_sec(phase) if phase != "other" else _arc_cap_sec("power")
            d = float(c.duration_sec)
            if d > cap + 0.4:
                capped.append(
                    ClipFragment(
                        trim_start_sec=c.trim_start_sec,
                        duration_sec=round(max(lo, cap), 2),
                        reason=c.reason,
                    )
                )
                notes.append(f"{phase or '段'}限长{cap:.0f}s")
            else:
                capped.append(c)

    out: list[ClipFragment] = []
    gap_need = _bridge_gap_sec()
    for i, c in enumerate(capped):
        if i > 0 and _arc_phase(c.reason or "") == "twist":
            prev = out[-1]
            prev_end = prev.trim_start_sec + prev.duration_sec
            gap = c.trim_start_sec - prev_end
            if gap > gap_need:
                cue = _find_cue_in_source_range(
                    cues, prev_end + 2.0, c.trim_start_sec - 1.0
                )
                if cue:
                    st = _cue_start(cue)
                    en = _cue_end(cue)
                    bd = round(max(lo, min(8.0, en - st + 0.5)), 2)
                    snippet = _cue_text(cue)[:16].replace("\n", " ")
                    out.append(
                        ClipFragment(
                            trim_start_sec=round(max(prev_end + 0.5, st - 0.2), 2),
                            duration_sec=bd,
                            reason=(
                                f"B|反转铺垫·过渡「{snippet}…」 "
                                f"start_sec={st:.2f} end_sec={en:.2f}"
                            ),
                        )
                    )
                    notes.append("反转前补B过渡")
        out.append(c)

    if out:
        last = out[-1]
        if _arc_phase(last.reason or "") == "hook" and not story_first:
            hook_cap = _arc_cap_sec("hook")
            reserve = 0.45
            nd = max(lo, min(last.duration_sec, hook_cap) - reserve)
            if nd < last.duration_sec - 0.25:
                out[-1] = ClipFragment(
                    trim_start_sec=last.trim_start_sec,
                    duration_sec=round(max(lo, nd), 2),
                    reason=last.reason,
                )
                notes.append("尾钩略缩短避片尾重复")

    out = _dedupe_and_sort_clips_simple(out)
    if target_sec > 0 and not story_first:
        out = restore_arc_body_total(
            out, target_sec=target_sec, cues=cues, dur_avail=dur_avail
        )
    if notes:
        logger.info(
            "叙事节奏：%s → %d 段 %.1fs",
            "，".join(dict.fromkeys(notes)),
            len(out),
            sum(c.duration_sec for c in out),
        )
    return out if out else capped


def _light_drop_transition_clips(clips: list) -> list:
    """仅剔除明显无信息过场，不改段数/入点/时长。"""
    if len(clips) <= 3:
        return clips
    from multi_clip import ClipFragment

    kept: list[ClipFragment] = []
    dropped = 0
    for c in sorted(clips, key=lambda x: x.trim_start_sec):
        if _is_transition_shot(c.reason or ""):
            dropped += 1
            continue
        kept.append(c)
    if kept and dropped:
        logger.info("轻量清理：去掉 %d 段无信息过场，保留 %d 段", dropped, len(kept))
    return kept if kept else list(clips)


def polish_clips_narrative_coherence(
    clips: list,
    cues: list,
    *,
    dur_avail: float,
) -> list:
    """强约束模式：删穿梭、压开篇、限后段碎跳（仅 NARRATIVE_COHESION=1 且非自主模式）。"""
    if not clips:
        return clips
    try:
        from hook_edit_methodology import ai_editor_autonomy_enabled

        if ai_editor_autonomy_enabled():
            return _light_drop_transition_clips(clips)
    except ImportError:
        pass
    if not narrative_cohesion_enabled():
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    avail = max(30.0, float(dur_avail))
    lo = clip_min_sec()
    sorted_clips = sorted(clips, key=lambda c: c.trim_start_sec)
    notes: list[str] = []

    kept: list[ClipFragment] = []
    dropped_trans = 0
    for c in sorted_clips:
        if _is_transition_shot(c.reason or "") and len(sorted_clips) > 3:
            dropped_trans += 1
            continue
        kept.append(c)
    if not kept:
        kept = list(sorted_clips)
    if dropped_trans:
        notes.append(f"删{dropped_trans}段穿梭/过场")

    anchor = _first_dialogue_anchor(cues)
    if anchor and kept:
        st, en, snippet = anchor
        first = kept[0]
        max_open = opening_first_clip_max_sec()
        if first.trim_start_sec < st - 1.5 or _is_transition_shot(first.reason or ""):
            dur = max(lo, min(max_open, en - st + 0.45))
            kept[0] = ClipFragment(
                trim_start_sec=round(max(0.0, st - 0.25), 2),
                duration_sec=round(dur, 2),
                reason=f"A|直入关键对白「{snippet}…」 start_sec={st:.2f} end_sec={en:.2f}",
            )
            notes.append(f"开篇对齐对白@{st:.1f}s")
        elif first.duration_sec > max_open + 0.5:
            first.duration_sec = round(max(lo, max_open), 2)
            notes.append(f"开篇压至{max_open:.0f}s")

    late_threshold = avail * late_start_ratio()
    early = [c for c in kept if c.trim_start_sec < late_threshold]
    late = [c for c in kept if c.trim_start_sec >= late_threshold]
    max_late = max_late_scatter_clips()
    if len(late) > max_late:
        late_sorted = sorted(
            late,
            key=lambda c: (
                0 if "悬念" in (c.reason or "") or "尾" in (c.reason or "") else 1,
                -c.duration_sec,
            ),
        )
        late = late_sorted[:max_late]
        notes.append(f"后段保留{max_late}段")
    kept = sorted(early + late, key=lambda c: c.trim_start_sec)

    max_n = max_cohesive_clip_count()
    if len(kept) > max_n:
        head = kept[: max_n - 1]
        tail = kept[-1:]
        kept = head + tail
        notes.append(f"收束为{max_n}段叙事")

    if notes:
        logger.info(
            "叙事连贯：%s → %d 段正片计划 %.1fs",
            "，".join(notes),
            len(kept),
            sum(c.duration_sec for c in kept),
        )
    return kept


def _is_opening_clip(c: Any) -> bool:
    r = (getattr(c, "reason", None) or "").strip()
    start = float(getattr(c, "trim_start_sec", 0) or 0)
    if start < opening_anchor_max_sec():
        return True
    markers = ("开场", "视听钩子", "前段高能", "fight+motion")
    return any(m in r for m in markers)


def enforce_opening_from_beats(
    clips: list,
    beats: list[dict[str, Any]],
    *,
    dur_avail: float,
) -> list:
    """AI 首段入点过晚时覆盖（仅非自主模式）。"""
    try:
        from hook_edit_methodology import ai_editor_autonomy_enabled

        if ai_editor_autonomy_enabled():
            return clips
    except ImportError:
        pass
    if not clips or not beats or not front_heavy_edit_enabled():
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    sorted_c = sorted(clips, key=lambda c: c.trim_start_sec)
    if sorted_c[0].trim_start_sec <= opening_anchor_max_sec():
        return clips

    early_beats = sorted(
        [b for b in beats if float(b.get("trim_start_sec") or 0) < dur_avail * 0.4],
        key=lambda b: float(b["trim_start_sec"]),
    )[:2]
    if not early_beats:
        return clips

    lo = clip_min_sec()
    anchored: list[ClipFragment] = []
    for b in early_beats:
        trim = max(0.0, min(float(b["trim_start_sec"]), dur_avail - lo - 0.5))
        dur = max(opening_clip_min_sec(), float(b.get("duration_sec") or lo))
        if trim + dur > dur_avail:
            dur = max(lo, dur_avail - trim)
        anchored.append(
            ClipFragment(
                trim_start_sec=round(trim, 2),
                duration_sec=round(dur, 2),
                reason=str(b.get("reason") or "A|开场视听钩子"),
            )
        )

    anchor_end = anchored[-1].trim_start_sec + anchored[-1].duration_sec
    rest = [
        c
        for c in sorted_c
        if c.trim_start_sec >= anchor_end - 2.0
        or c.trim_start_sec >= dur_avail * late_start_ratio()
    ]
    out = _dedupe_and_sort_clips_simple(anchored + rest)
    logger.info(
        "开场锚定：首段入点 %.1fs→%.1fs（算法草稿前段 %d 刀）",
        sorted_c[0].trim_start_sec,
        out[0].trim_start_sec if out else 0.0,
        len(anchored),
    )
    return out


def _dedupe_and_sort_clips_simple(clips: list) -> list:
    """按入点排序并去掉入点过近的重复段。"""
    if not clips:
        return clips
    out = sorted(clips, key=lambda c: c.trim_start_sec)
    merged: list = []
    for c in out:
        if merged and c.trim_start_sec < merged[-1].trim_start_sec + 3.0:
            if c.duration_sec > merged[-1].duration_sec:
                merged[-1] = c
            continue
        merged.append(c)
    return merged


def early_body_budget_ratio() -> float:
    """前段时长占正片目标的比例上限（非下限，后段仍有预算）。"""
    try:
        return min(0.88, max(0.55, float(os.getenv("HONGGUO_EARLY_BODY_RATIO", "0.68"))))
    except ValueError:
        return 0.68


@dataclass
class EpisodeEditBrief:
    episode_index: int
    label: str
    duration_sec: float
    structure_text: str
    emotion_arc: str
    few_shot_clips_json: str
    suggested_beats: list[dict[str, Any]] = field(default_factory=list)
    program_visual_clips: list[dict[str, Any]] = field(default_factory=list)

    def to_sidecar_text(self) -> str:
        prog = ""
        if self.program_visual_clips:
            prog = (
                "\n\n## 程序视听选段（无对白区）\n"
                + program_visual_block_for_prompt(self.program_visual_clips)
            )
        return "\n".join(
            [
                f"# {self.label} 剪辑简报",
                "",
                self.structure_text,
                prog,
                "",
                "## 推荐情绪线",
                self.emotion_arc,
                "",
                "## 推荐分镜（秒数可微调，clips 之和须等于 body duration_sec）",
                self.few_shot_clips_json,
            ]
        )


def _cue_text(cue: Any) -> str:
    if hasattr(cue, "text"):
        return str(cue.text or "")
    if isinstance(cue, dict):
        return str(cue.get("text") or "")
    return ""


def _cue_start(cue: Any) -> float:
    if hasattr(cue, "start_sec"):
        return float(cue.start_sec)
    return float(cue.get("start_sec", 0)) if isinstance(cue, dict) else 0.0


def _cue_end(cue: Any) -> float:
    if hasattr(cue, "end_sec"):
        return float(cue.end_sec)
    return float(cue.get("end_sec", 0)) if isinstance(cue, dict) else 0.0


def _vis_start(m: Any) -> float:
    if hasattr(m, "start_sec"):
        return float(m.start_sec)
    return float(m.get("start_sec", 0)) if isinstance(m, dict) else 0.0


def _vis_end(m: Any) -> float:
    if hasattr(m, "end_sec"):
        return float(m.end_sec)
    return float(m.get("end_sec", 0)) if isinstance(m, dict) else 0.0


def _vis_score(m: Any) -> float:
    if hasattr(m, "score"):
        return float(m.score)
    return float(m.get("score", 0)) if isinstance(m, dict) else 0.0


def _vis_tags(m: Any) -> str:
    if hasattr(m, "tags"):
        return str(m.tags or "")
    if isinstance(m, dict):
        return str(m.get("tags") or "")
    return ""


def dialogue_sparse_min_gap_sec() -> float:
    try:
        return max(2.0, float(os.getenv("HONGGUO_SILENT_GAP_MIN_SEC", "3.5")))
    except ValueError:
        return 3.5


def find_dialogue_sparse_windows(
    cues: list,
    duration: float,
) -> list[tuple[float, float, str]]:
    """对白稀少/空白时间窗：(起, 止, 说明)。"""
    dur = max(1.0, float(duration))
    min_gap = dialogue_sparse_min_gap_sec()
    windows: list[tuple[float, float, str]] = []
    if not cues:
        return [(0.0, min(dur, 18.0), "全片无对白")]
    first_tx = _cue_start(cues[0])
    if first_tx >= min_gap:
        windows.append((0.0, first_tx, "片头无对白"))
    for i in range(len(cues) - 1):
        gap_start = _cue_end(cues[i])
        gap_end = _cue_start(cues[i + 1])
        if gap_end - gap_start >= min_gap:
            windows.append((gap_start, gap_end, "对白间隙"))
    tail_start = _cue_end(cues[-1])
    if dur - tail_start >= min_gap * 1.5:
        windows.append((tail_start, dur, "片尾无对白"))
    return windows


def build_program_visual_clips(
    *,
    cues: list,
    visuals: list,
    duration: float,
) -> list[dict[str, Any]]:
    """无对白时段：由音浪+快切分析自动给出 clip 入点/时长（不传图给 LLM）。"""
    if not visuals:
        return []
    dur = max(30.0, float(duration))
    min_gap = dialogue_sparse_min_gap_sec()
    windows = find_dialogue_sparse_windows(cues, dur)
    out: list[dict[str, Any]] = []
    used: set[tuple[float, float]] = set()

    for win_start, win_end, win_label in windows:
        overlap = [
            v
            for v in visuals
            if _vis_end(v) > win_start + 0.2
            and _vis_start(v) < win_end - 0.2
            and _vis_score(v) >= 0.32
        ]
        if not overlap:
            overlap = [
                v
                for v in visuals
                if _vis_start(v) >= win_start - 1.5
                and _vis_start(v) <= win_end + 0.5
                and _vis_score(v) >= 0.38
            ]
        if not overlap:
            continue
        v = max(overlap, key=_vis_score)
        key = (round(_vis_start(v), 1), round(_vis_end(v), 1))
        if key in used:
            continue
        used.add(key)
        trim = max(0.0, _vis_start(v) - 0.15)
        span_end = min(dur, _vis_end(v))
        clip_dur = max(5.0, min(12.0, span_end - trim + 0.35))
        if trim + clip_dur > dur:
            clip_dur = max(4.0, dur - trim)
        tags = _vis_tags(v) or "motion+sfx_high"
        phase = "开场高能" if win_start < min(15.0, dur * 0.12) else "高能画面"
        out.append(
            {
                "trim_start_sec": round(trim, 2),
                "duration_sec": round(clip_dur, 2),
                "reason": (
                    f"A|{phase}·程序视听({win_label}) tags={tags} "
                    f"start_sec={_vis_start(v):.2f} end_sec={_vis_end(v):.2f}"
                ),
                "program": True,
            }
        )

    out.sort(key=lambda x: float(x["trim_start_sec"]))
    if out:
        logger.info(
            "程序视听选段 %d 段（无对白/间隙，由画面轴分析）",
            len(out),
        )
    return out


def program_visual_block_for_prompt(clips: list[dict[str, Any]]) -> str:
    if not clips:
        return ""
    lines = [
        "【程序视听选段 · 无对白/对白稀少时段，正片须优先采用下列秒数，勿凭想象改写】"
    ]
    for i, c in enumerate(clips, start=1):
        lines.append(
            f"{i}. trim_start_sec={c['trim_start_sec']} duration_sec={c['duration_sec']} | "
            f"{c.get('reason', '')}"
        )
    return "\n".join(lines)


def _opening_uses_program_visual(clips: list) -> bool:
    if not clips:
        return False
    first = min(clips, key=lambda c: float(getattr(c, "trim_start_sec", 0) or 0))
    r = (getattr(first, "reason", None) or "").strip()
    if float(getattr(first, "trim_start_sec", 0) or 0) > opening_anchor_max_sec():
        return False
    markers = (
        "motion",
        "fight",
        "sfx_high",
        "开场高能",
        "视听",
        "程序视听",
        "scene_cuts",
    )
    return any(m in r for m in markers)


def enforce_program_visual_opening(
    clips: list,
    program_clips: list[dict[str, Any]],
    cues: list,
    *,
    dur_avail: float,
) -> list:
    """AI 未选对无对白开场时，用程序画面轴覆盖首段。"""
    if not clips or not program_clips:
        return clips
    if _opening_uses_program_visual(clips):
        return clips
    if cues and _cue_start(cues[0]) < dialogue_sparse_min_gap_sec():
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    open_pick = min(
        program_clips,
        key=lambda c: float(c.get("trim_start_sec") or 0),
    )
    if float(open_pick.get("trim_start_sec") or 0) > opening_anchor_max_sec() + 1.0:
        return clips
    lo = clip_min_sec()
    prog = ClipFragment(
        trim_start_sec=float(open_pick["trim_start_sec"]),
        duration_sec=max(lo, float(open_pick["duration_sec"])),
        reason=str(open_pick.get("reason") or "A|开场高能·程序视听"),
    )
    sorted_c = sorted(clips, key=lambda c: c.trim_start_sec)
    anchor_end = prog.trim_start_sec + prog.duration_sec
    rest = [c for c in sorted_c if c.trim_start_sec >= anchor_end - 1.5]
    if len(rest) < len(sorted_c) - 1:
        rest = sorted_c[1:] if len(sorted_c) > 1 else sorted_c
    merged = [prog] + (rest if rest else sorted_c)
    logger.info(
        "无对白开场：程序画面轴覆盖首段 trim=%.2fs dur=%.2fs",
        prog.trim_start_sec,
        prog.duration_sec,
    )
    return merged


def _find_cues_with_keywords(cues: list, keywords: tuple[str, ...]) -> list[Any]:
    if not keywords:
        return []
    return [c for c in cues if any(k in _cue_text(c) for k in keywords)]


def _pick_strongest_cue_in_range(
    cues: list,
    duration: float,
    start_ratio: float,
    end_ratio: float,
) -> Any | None:
    """在片长区间内选信息量最大的一句对白（全品类通用，不依赖题材关键词）。"""
    if not cues or duration <= 0:
        return None
    lo = max(0.0, duration * start_ratio)
    hi = duration * end_ratio
    pool = [c for c in cues if lo <= _cue_start(c) < hi and len(_cue_text(c).strip()) >= 4]
    if not pool:
        return None
    return max(pool, key=lambda c: len(_cue_text(c).strip()))


def _phase_anchor(cues: list, keywords: tuple[str, ...]) -> Optional[float]:
    found = _find_cues_with_keywords(cues, keywords)
    return _cue_start(found[0]) if found else None


def _top_visual(visuals: list, n: int = 2) -> list[Any]:
    return sorted(visuals, key=lambda m: -_vis_score(m))[:n]


def _scale_beats_to_target(
    beats: list[dict[str, Any]],
    target: float,
    *,
    duration: float = 0.0,
) -> list[dict[str, Any]]:
    if not beats:
        return beats
    dur = max(30.0, float(duration or 0))
    threshold = dur * late_start_ratio() if dur > 0 else 9999.0

    if front_heavy_edit_enabled() and dur > 0:
        early = [b for b in beats if float(b["trim_start_sec"]) < threshold]
        late = [b for b in beats if float(b["trim_start_sec"]) >= threshold]
        early_budget = target * early_body_budget_ratio()
        late_budget = min(max_late_body_sec(), target - early_budget)
        parts: list[dict[str, Any]] = []
        for group, budget in ((early, early_budget), (late, late_budget)):
            if not group:
                continue
            subtotal = sum(float(b["duration_sec"]) for b in group)
            if subtotal < 0.3:
                continue
            r = budget / subtotal
            for b in group:
                d = max(2.0, round(float(b["duration_sec"]) * r, 1))
                parts.append({**b, "duration_sec": d})
        beats = sorted(parts, key=lambda x: float(x["trim_start_sec"]))

    total = sum(float(b["duration_sec"]) for b in beats)
    if total < 0.5:
        return beats
    ratio = target / total
    out = []
    for b in beats:
        d = max(2.0, round(float(b["duration_sec"]) * ratio, 1))
        out.append({**b, "duration_sec": d})
    drift = target - sum(x["duration_sec"] for x in out)
    if out and abs(drift) > 0.2:
        # 余量优先加到前段
        if front_heavy_edit_enabled():
            for x in out:
                if float(x["trim_start_sec"]) < threshold:
                    x["duration_sec"] = round(
                        float(x["duration_sec"]) + drift, 1
                    )
                    drift = 0
                    break
        if abs(drift) > 0.2:
            out[-1]["duration_sec"] = round(
                max(2.0, float(out[-1]["duration_sec"]) + drift), 1
            )
    return out


def build_suggested_beats(
    *,
    duration: float,
    cues: list,
    visuals: list,
    body_target_sec: float,
    program_visual_clips: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    dur = max(30.0, float(duration))
    beats: list[dict[str, Any]] = []

    program = program_visual_clips or build_program_visual_clips(
        cues=cues, visuals=visuals, duration=dur
    )
    if program:
        beats.append(program[0])
        for extra in program[1:3]:
            if float(extra["trim_start_sec"]) < dur * late_start_ratio():
                beats.append(extra)
    else:
        vis_ranked = sorted(visuals, key=lambda m: _vis_start(m)) if visuals else []
        vis_strong = [v for v in vis_ranked if _vis_score(v) >= 0.45]
        if vis_strong:
            v = vis_strong[0]
            s, e = _vis_start(v), min(_vis_end(v), _vis_start(v) + 11.0)
            beats.append(
                {
                    "trim_start_sec": round(max(0.5, s), 2),
                    "duration_sec": round(max(8.0, min(11.0, e - s)), 1),
                    "reason": f"A|开场高能 tags=fight+motion start_sec={s:.2f} end_sec={e:.2f}",
                }
            )
            for v2 in vis_strong[1:3]:
                s2, e2 = _vis_start(v2), min(_vis_end(v2), _vis_start(v2) + 14.0)
                if s2 < dur * late_start_ratio() and s2 > beats[-1]["trim_start_sec"] + 4.0:
                    beats.append(
                        {
                            "trim_start_sec": round(s2, 2),
                            "duration_sec": round(max(8.0, min(14.0, e2 - s2)), 1),
                            "reason": f"A|霸气叙事/高能画面 tags=motion+sfx_high start_sec={s2:.2f} end_sec={e2:.2f}",
                        }
                    )
                    break
        else:
            beats.append(
                {
                    "trim_start_sec": 0.88,
                    "duration_sec": 10.0,
                    "reason": "A|开场高能 tags=fight+motion start_sec=0.88 end_sec=11.00",
                }
            )

    _ARC_BEAT_RANGES = (
        (0.10, 0.38, "A|霸气叙事"),
        (0.68, 0.98, "A|尾钩引流"),
    )
    for start_r, end_r, label in _ARC_BEAT_RANGES:
        cue = _pick_strongest_cue_in_range(cues, dur, start_r, end_r)
        if not cue:
            continue
        s, e = _cue_start(cue), _cue_end(cue)
        snippet = _cue_text(cue)[:20].replace("\n", " ")
        beats.append(
            {
                "trim_start_sec": round(max(0.0, s - 0.2), 2),
                "duration_sec": round(max(6.0, min(14.0, e - s + 0.6)), 1),
                "reason": (
                    f"{label}「{snippet}…」 start_sec={s:.2f} end_sec={e:.2f}"
                ),
            }
        )

    twist_hits = _find_cues_with_keywords(
        cues, ("原来", "真相", "没想到", "竟然", "假的", "骗")
    )
    if twist_hits:
        cue = twist_hits[0]
        s, e = _cue_start(cue), _cue_end(cue)
        snippet = _cue_text(cue)[:20].replace("\n", " ")
        beats.append(
            {
                "trim_start_sec": round(max(0.0, s - 0.2), 2),
                "duration_sec": round(max(6.0, min(12.0, e - s + 0.6)), 1),
                "reason": (
                    f"A|反转「{snippet}…」 start_sec={s:.2f} end_sec={e:.2f}"
                ),
            }
        )
    else:
        cue = _pick_strongest_cue_in_range(cues, dur, 0.40, 0.72)
        if cue:
            s, e = _cue_start(cue), _cue_end(cue)
            snippet = _cue_text(cue)[:20].replace("\n", " ")
            beats.append(
                {
                    "trim_start_sec": round(max(0.0, s - 0.2), 2),
                    "duration_sec": round(max(6.0, min(12.0, e - s + 0.6)), 1),
                    "reason": (
                        f"A|反转「{snippet}…」 start_sec={s:.2f} end_sec={e:.2f}"
                    ),
                }
            )

    if len(beats) > 6:
        beats = [beats[0], beats[1], beats[2], beats[-2], beats[-1]]
    return _scale_beats_to_target(beats, body_target_sec, duration=dur)


def build_episode_structure_text(
    *,
    label: str,
    duration: float,
    cues: list,
    visuals: list,
) -> tuple[str, str]:
    dur = float(duration)
    lines = [f"### {label} 结构地图（片长 {dur:.0f}s）", ""]
    first_tx = _cue_start(cues[0]) if cues else dur
    if first_tx > 8.0:
        lines.append(
            f"- **0–{first_tx:.0f}s**：开场以画面/音效为主（对白稀少），"
            f"必须用画面/音效轴选段，勿等台词。"
        )
        vis_early = [v for v in visuals if _vis_end(v) <= first_tx + 5]
        if vis_early:
            v = max(vis_early, key=_vis_score)
            lines.append(
                f"  - 推荐视听钩子：{_vis_start(v):.1f}–{_vis_end(v):.1f}s "
                f"（tags 见画面轴）"
            )

    for phase_name, kws in _PHASE_RULES:
        anchor = _phase_anchor(cues, kws)
        if anchor is None:
            continue
        sample = _cue_text(_find_cues_with_keywords(cues, kws)[0])[:28]
        lines.append(f"- **约 {anchor:.0f}s 起 · {phase_name}**：「{sample}…」")

    if visuals:
        v = _top_visual(visuals, 1)[0]
        lines.append(
            f"- **最强视听段**：{_vis_start(v):.1f}–{_vis_end(v):.1f}s "
            f"(score={_vis_score(v):.2f})"
        )

    threshold = dur * late_start_ratio()
    early_pct = int(round(early_body_budget_ratio() * 100))
    arc = (
        "【六步叙事 · 本集须覆盖】流畅衔接 → 开场高能 → 霸气叙事立势 → 反转 → 尾钩引流 → 完整闭环。"
        f"前段（约占正文 {early_pct}%）侧重高能+霸气；中段找反转；末段（约 {threshold:.0f}s 后，"
        f"{min_late_body_sec():.0f}–{max_late_body_sec():.0f}s）留尾钩，勿剧透光结局。"
        "题材差异只影响选哪句对白/哪段画面，骨架不变。"
    )
    return "\n".join(lines), arc


def build_episode_edit_brief(
    *,
    episode_index: int,
    label: str,
    duration: float,
    cues: list,
    visuals: list,
    body_target_sec: float,
) -> EpisodeEditBrief:
    structure, arc = build_episode_structure_text(
        label=label, duration=duration, cues=cues, visuals=visuals
    )
    program_clips = build_program_visual_clips(
        cues=cues, visuals=visuals, duration=duration
    )
    if program_clips:
        structure = (
            structure
            + "\n\n"
            + program_visual_block_for_prompt(program_clips)
        )
    beats = build_suggested_beats(
        duration=duration,
        cues=cues,
        visuals=visuals,
        body_target_sec=body_target_sec,
        program_visual_clips=program_clips,
    )
    few_shot = json.dumps(beats, ensure_ascii=False, indent=2)
    return EpisodeEditBrief(
        episode_index=episode_index,
        label=label,
        duration_sec=duration,
        structure_text=structure,
        emotion_arc=arc,
        few_shot_clips_json=few_shot,
        suggested_beats=beats,
        program_visual_clips=program_clips,
    )


def briefs_block_for_prompt(briefs: dict[int, EpisodeEditBrief]) -> str:
    if not briefs:
        return ""
    parts = [b.structure_text + "\n" + b.emotion_arc for b in briefs.values()]
    return (
        "\n\n【分集结构地图 · 全品类短剧，剪之前先读本集对白/画面轴】\n"
        + "\n\n".join(parts)
        + "\n"
    )


def few_shot_block_for_prompt(
    briefs: dict[int, EpisodeEditBrief], body_target_sec: float
) -> str:
    if not briefs:
        return ""
    b = briefs.get(1) or next(iter(briefs.values()))
    return (
        f"\n【算法草稿分镜 · 仅供参考，须按本集对白/画面轴自主重写】\n"
        f"可改段数、入点、时长；目标：流畅·高能开场·霸气·反转·尾钩·闭环。\n"
        f'"clips": {b.few_shot_clips_json}\n'
    )


def gather_episode_edit_briefs(
    *,
    episode_labels: list[str],
    episode_durations: list[float],
    episode_transcripts: dict[int, list] | None,
    episode_visual_profiles: dict[int, list] | None,
    body_target_sec: float,
    work_dir: Path | None = None,
) -> dict[int, EpisodeEditBrief]:
    out: dict[int, EpisodeEditBrief] = {}
    indices = set()
    if episode_transcripts:
        indices.update(episode_transcripts.keys())
    if episode_visual_profiles:
        indices.update(episode_visual_profiles.keys())
    for idx in sorted(indices):
        label = (
            episode_labels[idx - 1]
            if idx - 1 < len(episode_labels)
            else f"第{idx}集"
        )
        dur = (
            episode_durations[idx - 1]
            if idx - 1 < len(episode_durations)
            else 120.0
        )
        cues = (episode_transcripts or {}).get(idx) or []
        visuals = (episode_visual_profiles or {}).get(idx) or []
        brief = build_episode_edit_brief(
            episode_index=idx,
            label=label,
            duration=dur,
            cues=cues,
            visuals=visuals,
            body_target_sec=body_target_sec,
        )
        out[idx] = brief
        if work_dir:
            path = work_dir / f"edit_brief_ep{idx:02d}.md"
            path.write_text(brief.to_sidecar_text(), encoding="utf-8")
        logger.info("剪辑简报 %s：%d 段推荐分镜", label, len(brief.suggested_beats))
    return out


def _parse_times_from_reason(reason: str) -> tuple[Optional[float], Optional[float]]:
    starts: list[float] = []
    ends: list[float] = []
    for m in _SEC_RE.finditer(reason or ""):
        if m.group(1):
            starts.append(float(m.group(1)))
        if m.group(2):
            ends.append(float(m.group(2)))
        if m.group(3) and m.group(4):
            a, b = float(m.group(3)), float(m.group(4))
            starts.append(min(a, b))
            ends.append(max(a, b))
    st = starts[0] if starts else None
    en = ends[-1] if ends else None
    if st is not None and en is not None and en < st:
        return st, None
    return st, en


def _clip_topics(reason: str) -> set[str]:
    """从 reason 提取主题词，用于去重（不绑定某一题材词表）。"""
    r = re.sub(r"^[AB]\s*[|｜]\s*", "", (reason or "").strip(), flags=re.I)
    r = re.sub(r"start_sec\s*=\s*[0-9.]+|end_sec\s*=\s*[0-9.]+", "", r, flags=re.I)
    r = re.sub(r"tags\s*=\s*[\w+]+", "", r, flags=re.I)
    tokens = set(re.findall(r"[\u4e00-\u9fff]{2,8}", r))
    return {t for t in tokens if t not in _TOPIC_STOPWORDS}


def rebalance_clips_front_heavy(
    clips: list,
    *,
    dur_avail: float,
    target_sec: float,
) -> list:
    """略偏前段，但后段保留冲突/铺垫时长带，避免后段被压成一闪而过。"""
    if not front_heavy_edit_enabled() or not clips:
        return clips
    from multi_clip import ClipFragment, clip_min_sec

    threshold = float(dur_avail) * late_start_ratio()
    max_late = max_late_body_sec()
    min_late = min(min_late_body_sec(), max_late)
    early_cap = float(target_sec) * early_body_budget_ratio()
    lo = clip_min_sec()
    sorted_clips = sorted(clips, key=lambda c: c.trim_start_sec)
    early = [c for c in sorted_clips if c.trim_start_sec < threshold]
    late = [c for c in sorted_clips if c.trim_start_sec >= threshold]

    late_sum = sum(c.duration_sec for c in late)
    if late_sum > max_late + 0.1 and late:
        scale = max_late / late_sum
        for c in late:
            c.duration_sec = max(lo, round(c.duration_sec * scale, 2))
        logger.info(
            "节奏平衡：后段(≥%.0fs) 由 %.1fs 压至 %.1fs（上限 %.0fs）",
            threshold,
            late_sum,
            sum(c.duration_sec for c in late),
            max_late,
        )

    total = sum(c.duration_sec for c in sorted_clips)
    need = float(target_sec) - total
    early_sum = sum(c.duration_sec for c in early)
    late_sum = sum(c.duration_sec for c in late)

    if need > 0.4:
        if early and early_sum < early_cap - 0.1:
            room = min(need, early_cap - early_sum)
            add_each = room / len(early)
            for c in early:
                c.duration_sec = round(c.duration_sec + add_each, 2)
            need -= room
        if need > 0.4 and late:
            add_each = need / len(late)
            for c in late:
                c.duration_sec = round(c.duration_sec + add_each, 2)
        elif need > 0.4 and early:
            add_each = need / len(early)
            for c in early:
                c.duration_sec = round(c.duration_sec + add_each, 2)
    elif need < -0.4:
        over = -need
        if early and early_sum > early_cap + 0.1:
            for c in reversed(early):
                floor = opening_clip_min_sec() if _is_opening_clip(c) else lo
                cut = min(over, max(0.0, c.duration_sec - floor))
                c.duration_sec = round(c.duration_sec - cut, 2)
                over -= cut
                if over <= 0:
                    break
        late_sum = sum(c.duration_sec for c in late)
        if over > 0 and late and late_sum > min_late + 0.1:
            for c in reversed(late):
                cut = min(over, max(0.0, c.duration_sec - lo))
                c.duration_sec = round(c.duration_sec - cut, 2)
                over -= cut
                if over <= 0 or sum(x.duration_sec for x in late) <= min_late:
                    break

    return sorted(early + late, key=lambda c: c.trim_start_sec)


def dedupe_narrative_clips(clips: list, *, min_topic_gap: float = 28.0) -> list:
    """合并主题重复且入点过近的 clip（同段情节勿剪两遍）。"""
    if len(clips) < 2:
        return clips
    from multi_clip import ClipFragment

    sorted_clips = sorted(clips, key=lambda c: c.trim_start_sec)
    out: list = []
    for c in sorted_clips:
        topics = _clip_topics(c.reason or "")
        drop = False
        for prev in out:
            if abs(c.trim_start_sec - prev.trim_start_sec) < min_topic_gap:
                if topics and topics & _clip_topics(prev.reason or ""):
                    logger.info(
                        "去重：丢弃与 %.1fs 段主题重复的 clip %.1fs",
                        prev.trim_start_sec,
                        c.trim_start_sec,
                    )
                    drop = True
                    break
        if not drop:
            out.append(c)
    return out


def repair_clip_durations_in_raw(
    raw: dict[str, Any],
    *,
    dur_avail: float,
    body_target_sec: float,
) -> int:
    """校验前：按 reason 的 end_sec 拉长 clip，同步 body duration，抬过短段。"""
    try:
        from multi_clip import clip_min_sec
    except ImportError:
        return 0
    try:
        from edge_tts_narration import dialogue_tail_pad_sec

        pad = dialogue_tail_pad_sec()
    except ImportError:
        pad = 0.45
    try:
        from hook_timeline import story_first_edit_enabled

        story_first = story_first_edit_enabled()
    except ImportError:
        story_first = False

    lo = clip_min_sec()
    avail = max(lo + 1.0, float(dur_avail) - 0.5)
    tol = duration_tolerance_sec()
    fixes = 0
    for seg in raw.get("body_segments") or []:
        if not isinstance(seg, dict):
            continue
        clips = seg.get("clips") or []
        if not isinstance(clips, list):
            continue
        body_dur = float(seg.get("duration_sec") or body_target_sec)
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            reason = str(clip.get("reason") or "")
            st_r, en_r = _parse_times_from_reason(reason)
            trim = float(clip.get("trim_start_sec") or 0)
            dur = float(clip.get("duration_sec") or 0)
            if st_r is not None and abs(trim - st_r) < 10.0:
                trim = max(0.0, st_r - 0.25)
                clip["trim_start_sec"] = round(trim, 2)
            if en_r is not None and en_r > trim:
                reason_max = en_r - trim + pad
                if reason_max < lo:
                    need = max(0.5, reason_max)
                elif dur < reason_max - 0.35:
                    need = max(lo, reason_max)
                else:
                    need = max(lo, min(dur, reason_max))
                if need > dur + 0.35 and trim + need <= avail:
                    clip["duration_sec"] = round(need, 2)
                    fixes += 1
                    dur = need
            if dur >= lo - 0.05 or dur <= 0:
                continue
            new_dur = max(lo, min(avail - trim, lo))
            clip["duration_sec"] = round(new_dur, 2)
            fixes += 1
        total = sum(
            float(c.get("duration_sec") or 0)
            for c in clips
            if isinstance(c, dict)
        )
        if story_first and clips:
            from multi_clip import ClipFragment

            frag = [
                ClipFragment(
                    trim_start_sec=float(c.get("trim_start_sec") or 0),
                    duration_sec=float(c.get("duration_sec") or 0),
                    reason=str(c.get("reason") or ""),
                )
                for c in clips
                if isinstance(c, dict)
            ]
            capped = _cap_clips_body_span(frag, dur_avail=avail)
            if capped and sum(c.duration_sec for c in capped) < total - 0.05:
                for clip, frag_c in zip(
                    (c for c in clips if isinstance(c, dict)), capped
                ):
                    clip["duration_sec"] = frag_c.duration_sec
                total = sum(c.duration_sec for c in capped)
                fixes += 1
        if story_first or abs(total - body_dur) > tol:
            seg["duration_sec"] = round(total, 2)
        body_dur = float(seg.get("duration_sec") or total)
        if story_first or total <= body_dur + tol:
            continue
        if total > body_dur + tol:
            over = total - body_dur
            for clip in sorted(
                (c for c in clips if isinstance(c, dict)),
                key=lambda x: float(x.get("duration_sec") or 0),
                reverse=True,
            ):
                d = float(clip.get("duration_sec") or 0)
                if d <= lo + 0.05:
                    continue
                cut = min(over, d - lo)
                clip["duration_sec"] = round(d - cut, 2)
                over -= cut
                if over <= tol:
                    break
            seg["duration_sec"] = round(
                sum(float(c.get("duration_sec") or 0) for c in clips if isinstance(c, dict)),
                2,
            )
    return fixes


def _ai_hook_only_enabled() -> bool:
    v = os.getenv("HONGGUO_AI_HOOK_ONLY", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _hook_plan_max_clips() -> int:
    raw = os.getenv("HONGGUO_HOOK_PLAN_MAX_CLIPS", "7").strip()
    try:
        return max(3, min(12, int(raw)))
    except ValueError:
        return 7


def _hook_plan_clip_bounds() -> tuple[float, float]:
    lo = float(os.getenv("HONGGUO_HOOK_CLIP_MIN_SEC", "2.5").strip() or "2.5")
    hi = float(os.getenv("HONGGUO_HOOK_CLIP_MAX_SEC", "5.5").strip() or "5.5")
    return max(1.5, lo), max(lo + 0.5, hi)


def _truncate_reason(reason: str, *, max_len: int = 36) -> str:
    r = re.sub(r"\s+", " ", (reason or "").strip())
    if len(r) <= max_len:
        return r
    return r[: max_len - 1].rstrip() + "…"


def _reason_signature(reason: str) -> str:
    r = (reason or "").strip()
    r = re.sub(r"^A[|｜]\s*", "", r)
    r = re.sub(r"^B[|｜]\s*", "", r)
    r = re.sub(r"tags\s*=\s*[\w/,]+", "", r, flags=re.I)
    r = re.sub(r"start_sec\s*=\s*[0-9.]+", "", r, flags=re.I)
    r = re.sub(r"end_sec\s*=\s*[0-9.]+", "", r, flags=re.I)
    return re.sub(r"\s+", "", r)[:18]


def _hook_clip_dict_score(clip: dict[str, Any]) -> float:
    dur = float(clip.get("duration_sec") or 0)
    reason = str(clip.get("reason") or "")
    score = 0.0
    if reason.startswith(("A|", "A｜")):
        score += 4.0
    elif reason.startswith(("B|", "B｜")):
        score += 1.5
    lo, hi = _hook_plan_clip_bounds()
    if lo <= dur <= hi:
        score += 3.0
    elif dur > hi + 2.0:
        score -= min(6.0, (dur - hi) * 0.8)
    elif dur < lo:
        score -= 4.0
    for kw in ("反转", "冲突", "对峙", "打脸", "悬念", "高能", "尾钩"):
        if kw in reason:
            score += 0.6
    tags = clip.get("tags")
    if isinstance(tags, list) and tags:
        score += min(1.5, 0.25 * len(tags))
    return score


def _normalize_clip_dict_times(clip: dict[str, Any], *, dur_avail: float) -> bool:
    """补齐 trim/duration；无效段返回 False。"""
    if not isinstance(clip, dict):
        return False
    start = float(clip.get("trim_start_sec") or clip.get("start_sec") or 0)
    dur = float(clip.get("duration_sec") or clip.get("duration") or 0)
    end_raw = clip.get("end_sec")
    if end_raw is not None:
        try:
            end_v = float(end_raw)
            if end_v > start + 0.35 and dur < 0.45:
                dur = end_v - start
        except (TypeError, ValueError):
            pass
    if dur < 0.45:
        return False
    avail = max(5.0, float(dur_avail) - 0.5)
    start = max(0.0, min(start, avail - 0.5))
    dur = max(0.45, min(dur, avail - start))
    clip["trim_start_sec"] = round(start, 2)
    clip.pop("start_sec", None)
    clip["duration_sec"] = round(dur, 2)
    clip.pop("end_sec", None)
    clip["reason"] = _truncate_reason(str(clip.get("reason") or ""))
    return True


def coerce_llm_edit_raw(
    raw: dict[str, Any],
    *,
    episode_count: int = 1,
    body_target_sec: float = 23.0,
    dur_avail: float = 600.0,
) -> int:
    """将模型常见非标 JSON 转为 body_segments 结构。返回修正项数。"""
    fixes = 0
    if not isinstance(raw, dict):
        return 0

    hs = raw.get("hook_summary")
    if isinstance(hs, list):
        raw["hook_summary"] = "；".join(
            str(x).strip() for x in hs if str(x).strip()
        )[:240]
        fixes += 1
    elif hs is not None and not isinstance(hs, str):
        raw["hook_summary"] = str(hs).strip()[:240]
        fixes += 1

    segments = raw.get("body_segments")
    root_clips = raw.get("clips")
    if (not isinstance(segments, list) or not segments) and isinstance(
        root_clips, list
    ):
        body_dur = float(
            raw.get("body_duration_sec")
            or raw.get("duration_sec")
            or body_target_sec
        )
        raw["body_segments"] = [
            {
                "episode_index": 1,
                "duration_sec": body_dur,
                "clips": root_clips,
                "reason": "全钩子快切",
            }
        ]
        fixes += 1
        segments = raw["body_segments"]

    if isinstance(segments, list):
        for i, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            if not seg.get("episode_index"):
                seg["episode_index"] = min(i + 1, max(1, episode_count))
                fixes += 1
            clips = seg.get("clips")
            if not isinstance(clips, list):
                continue
            cleaned: list[dict[str, Any]] = []
            for c in clips:
                if not isinstance(c, dict):
                    continue
                if _normalize_clip_dict_times(c, dur_avail=dur_avail):
                    cleaned.append(c)
                else:
                    fixes += 1
            seg["clips"] = cleaned
            if cleaned:
                total = sum(float(x.get("duration_sec") or 0) for x in cleaned)
                seg["duration_sec"] = round(total, 2)
    return fixes


def sanitize_hook_only_plan_raw(
    raw: dict[str, Any],
    *,
    body_target_sec: float,
    dur_avail: float,
) -> int:
    """钩子模式：裁段数/单段时长/总时长，去重 reason，不信模型原值。"""
    if not _ai_hook_only_enabled():
        return 0
    try:
        from hook_timeline import story_first_edit_enabled

        if story_first_edit_enabled():
            return 0
    except ImportError:
        pass

    try:
        from hook_timeline import ai_body_script_max_sec

        cap_total = min(body_target_sec, ai_body_script_max_sec())
    except ImportError:
        cap_total = body_target_sec

    max_clips = _hook_plan_max_clips()
    lo, hi = _hook_plan_clip_bounds()
    avail = max(lo + 1.0, float(dur_avail) - 0.5)
    fixes = 0

    for seg in raw.get("body_segments") or []:
        if not isinstance(seg, dict):
            continue
        clips = seg.get("clips") or []
        if not isinstance(clips, list):
            continue
        normed: list[dict[str, Any]] = []
        seen_pairs: list[tuple[str, float]] = []
        for c in clips:
            if not isinstance(c, dict):
                continue
            if not _normalize_clip_dict_times(c, dur_avail=avail):
                fixes += 1
                continue
            sig = _reason_signature(str(c.get("reason") or ""))
            trim = float(c.get("trim_start_sec") or 0)
            if sig and any(
                abs(trim - t) < 4.0 and sig == s for s, t in seen_pairs
            ):
                fixes += 1
                continue
            if any(abs(trim - t) < 1.0 for _, t in seen_pairs):
                fixes += 1
                continue
            dur = float(c.get("duration_sec") or 0)
            if dur > hi:
                c["duration_sec"] = round(hi, 2)
                fixes += 1
            elif dur < lo:
                c["duration_sec"] = round(lo, 2)
                fixes += 1
            if sig:
                seen_pairs.append((sig, trim))
            normed.append(c)

        if len(normed) > max_clips:
            normed = sorted(
                normed,
                key=lambda x: (
                    -_hook_clip_dict_score(x),
                    float(x.get("trim_start_sec") or 0),
                ),
            )[:max_clips]
            normed = sorted(normed, key=lambda x: float(x.get("trim_start_sec") or 0))
            fixes += 1

        total = sum(float(c.get("duration_sec") or 0) for c in normed)
        if total < cap_total * 0.88 and normed:
            ratio = min(1.45, cap_total / max(total, 0.01))
            for c in normed:
                d = float(c.get("duration_sec") or lo)
                c["duration_sec"] = round(min(hi, max(lo, d * ratio)), 2)
            fixes += 1
            total = sum(float(c.get("duration_sec") or 0) for c in normed)
        if total > cap_total + 0.35 and normed:
            ratio = cap_total / max(total, 0.01)
            for c in normed:
                d = float(c.get("duration_sec") or lo)
                c["duration_sec"] = round(max(lo, min(hi, d * ratio)), 2)
            fixes += 1
            total = sum(float(c.get("duration_sec") or 0) for c in normed)
            while total > cap_total + 0.35 and len(normed) > 3:
                drop = min(normed, key=_hook_clip_dict_score)
                normed.remove(drop)
                fixes += 1
                total = sum(float(c.get("duration_sec") or 0) for c in normed)

        seg["clips"] = normed
        seg["duration_sec"] = round(total, 2)
        if normed:
            logger.info(
                "钩子模式硬裁：保留 %d 段，正片 %.1fs（上限 %.0fs，单段 %.1f–%.1fs）",
                len(normed),
                total,
                cap_total,
                lo,
                hi,
            )
    return fixes


def validate_ai_edit_raw(
    raw: dict[str, Any],
    *,
    body_target_sec: float,
    dur_avail: float,
) -> list[str]:
    errors: list[str] = []
    tol = duration_tolerance_sec()
    max_gap = max_clip_gap_sec()
    segments = raw.get("body_segments") or []
    if not isinstance(segments, list) or not segments:
        errors.append("缺少 body_segments")
        return errors

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        idx = seg.get("episode_index", "?")
        body_dur = float(seg.get("duration_sec") or body_target_sec)
        clips = seg.get("clips") or []
        if not isinstance(clips, list) or not clips:
            errors.append(f"第{idx}集 clips 为空")
            continue

        sorted_clip_list = sorted(
            clips, key=lambda x: float(x.get("trim_start_sec", 0))
        )
        if sorted_clip_list:
            skip_opening_anchor = False
            try:
                from hook_edit_methodology import ai_editor_autonomy_enabled

                skip_opening_anchor = ai_editor_autonomy_enabled()
            except ImportError:
                pass
            if not skip_opening_anchor:
                first_trim = float(sorted_clip_list[0].get("trim_start_sec") or 0)
                if first_trim > opening_anchor_max_sec():
                    errors.append(
                        f"第{idx}集 首段须从片头 {opening_anchor_max_sec():.0f}s 内起切"
                        f"（开场视听钩子），当前 {first_trim:.0f}s"
                    )

        total = 0.0
        prev_end = 0.0
        for i, clip in enumerate(sorted_clip_list):
            if not isinstance(clip, dict):
                continue
            trim = float(clip.get("trim_start_sec") or 0)
            dur = float(clip.get("duration_sec") or 0)
            total += dur
            try:
                from multi_clip import clip_min_sec

                min_d = clip_min_sec()
            except ImportError:
                min_d = 6.0
            if dur < min_d - 0.15:
                errors.append(
                    f"第{idx}集 clip#{i+1} duration_sec={dur:.1f}s 过短（最少 {min_d:.0f}s）"
                )
            if trim + dur > dur_avail + 1.0:
                errors.append(
                    f"第{idx}集 clip#{i+1} 超出片长（{trim + dur:.1f}s > {dur_avail:.0f}s）"
                )
            reason = str(clip.get("reason") or "")
            st, en = _parse_times_from_reason(reason)
            src = str(clip.get("source_time") or "")
            if src and "-" in src:
                parts = src.replace("s", "").split("-")
                try:
                    a, b = float(parts[0]), float(parts[1])
                    if b < a:
                        errors.append(
                            f"第{idx}集 clip#{i+1} source_time 颠倒：{src}"
                        )
                except ValueError:
                    pass
            if st is not None and en is not None and en < st:
                errors.append(
                    f"第{idx}集 clip#{i+1} reason 中 end_sec < start_sec"
                )
            gap = trim - prev_end
            if (
                clip_gap_strict_validation()
                and i > 0
                and gap > max_gap
                and not _reason_allows_source_gap(reason)
            ):
                errors.append(
                    f"第{idx}集 clip#{i+1} 与上一段间隔 {gap:.0f}s > {max_gap:.0f}s，"
                    f"须在 reason 写「跳剪/跳切」或 B| 过渡，或改选中间素材"
                )
            elif i > 0 and gap > max_gap and not _reason_allows_source_gap(reason):
                logger.debug(
                    "第%s集 clip#%d 间隔 %.0fs（已放宽，不阻断生成）",
                    idx,
                    i + 1,
                    gap,
                )
            prev_end = trim + dur

        if abs(total - body_dur) > tol:
            errors.append(
                f"第{idx}集 clips 合计 {total:.1f}s ≠ body duration_sec {body_dur:.1f}s "
                f"(允许 ±{tol}s)"
            )
        if abs(body_dur - body_target_sec) > tol + 1.0:
            allow_longer = False
            try:
                from hook_timeline import story_first_edit_enabled

                if story_first_edit_enabled():
                    allow_longer = True
            except ImportError:
                pass
            if not allow_longer:
                try:
                    from edge_tts_narration import (
                        dialogue_compress_grace_sec,
                        dialogue_completeness_enabled,
                    )

                    grace = dialogue_compress_grace_sec()
                    if (
                        dialogue_completeness_enabled()
                        and total <= body_target_sec + grace + tol
                        and abs(total - body_dur) <= tol
                        and body_dur >= body_target_sec - tol
                    ):
                        allow_longer = True
                except ImportError:
                    pass
            if not allow_longer:
                errors.append(
                    f"第{idx}集 body duration_sec 应为 {body_target_sec:.1f}s 左右"
                )

        skip_late_cap = False
        try:
            from hook_edit_methodology import ai_editor_autonomy_enabled
            from hook_timeline import story_first_edit_enabled

            skip_late_cap = ai_editor_autonomy_enabled() or story_first_edit_enabled()
        except ImportError:
            pass
        if front_heavy_edit_enabled() and not skip_late_cap:
            threshold = float(dur_avail) * late_start_ratio()
            late_dur = sum(
                float(c.get("duration_sec") or 0)
                for c in clips
                if isinstance(c, dict)
                and float(c.get("trim_start_sec") or 0) >= threshold
            )
            if late_dur > max_late_body_sec() + tol:
                errors.append(
                    f"第{idx}集 后段(≥{threshold:.0f}s) 合计 {late_dur:.1f}s，"
                    f"超过上限 {max_late_body_sec():.0f}s；可缩短长回忆，但保留冲突/悬念铺垫"
                )

    return errors


def build_validation_retry_prompt(
    errors: list[str],
    previous_json: str,
    *,
    body_target_sec: float,
) -> str:
    err_lines = "\n".join(f"- {e}" for e in errors)
    try:
        from hook_edit_methodology import ai_editor_autonomy_enabled

        if ai_editor_autonomy_enabled():
            fix_hint = (
                f"修正要求：clips.duration_sec 之和 = body duration_sec（约 {body_target_sec:.1f}s，"
                "可略长以保对白完整）；每条 start_sec ≤ end_sec；"
                f"大跳剪间隔>{max_clip_gap_sec():.0f}s 须在 reason 标跳剪；"
                "按本集轴重写秒数，保持简版叙事完整，勿套固定分镜模板。"
            )
        else:
            fix_hint = (
                f"修正要求：clips.duration_sec 之和必须等于 body_segments[].duration_sec"
                f"（目标 {body_target_sec:.1f}s，误差≤0.5）；"
                f"每条 reason 的 start_sec ≤ end_sec；源片相邻段间隔>{max_clip_gap_sec():.0f}s 须在 reason 标跳剪/B|；"
                "不要重复两段讲同一情节/主题；"
                f"后段合计 {min_late_body_sec():.0f}–{max_late_body_sec():.0f}s，"
                f"前段爽点约占正文 {int(early_body_budget_ratio() * 100)}% 内；"
                "前两刀：片头 0~3s 起切约 10~12s 视听钩子 + 约 14s 起前段高能，禁止首段从 20s 后才开始；"
                "后段保留冲突/轻虐/悬念铺垫，勿压成 1s 闪切。"
            )
    except ImportError:
        fix_hint = (
            f"修正要求：clips.duration_sec 之和必须等于 body duration_sec（目标 {body_target_sec:.1f}s）。"
        )
    return (
        "你上一版 JSON 有以下硬错误，请只输出修正后的完整 JSON（不要 markdown）：\n"
        f"{err_lines}\n\n"
        f"{fix_hint}\n\n"
        f"上一版：\n{previous_json[:12000]}"
    )
