"""专业 30 秒强钩子：片头口播 + 23s 冲突高光正片 + 片尾 CTA。"""

from __future__ import annotations

import os

from hook_duration_budget import is_one_minute_hook_preset


def pro_60_template_enabled() -> bool:
    t = os.getenv("HONGGUO_HOOK_TEMPLATE", "").strip().lower()
    if t in ("pro_60", "pro60", "professional_60", "pro"):
        return True
    return is_one_minute_hook_preset()


def ai_body_faithful_enabled() -> bool:
    """正片只按 AI 分镜剪（入点+时长），不压到 body_main_sec、不做前重后轻配方。"""
    v = os.getenv("HONGGUO_AI_BODY_FAITHFUL", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    if not pro_60_template_enabled():
        return False
    try:
        from multi_clip import ai_clip_strict_enabled

        return ai_clip_strict_enabled()
    except ImportError:
        return True


def story_first_edit_enabled() -> bool:
    """故事完整优先：时长由 AI 据对白/情节决定，不以 .env 秒数为成片标准。"""
    v = os.getenv("HONGGUO_STORY_FIRST_EDIT", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    try:
        from edge_tts_narration import dialogue_completeness_enabled

        return dialogue_completeness_enabled() and ai_body_faithful_enabled()
    except ImportError:
        return ai_body_faithful_enabled()


def _read_seg(key: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return max(lo, min(hi, v))


def opening_voiceover_enabled() -> bool:
    """片头口播开关：关=不生成片头口播段，直接进入正片。"""
    v = os.getenv("HONGGUO_OPENING_VOICEOVER", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def outro_voiceover_enabled() -> bool:
    """片尾口播开关：关=正片结束即成片，不追加尾帧 TTS。"""
    v = os.getenv("HONGGUO_OUTRO_VOICEOVER", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def golden_open_sec() -> float:
    if not opening_voiceover_enabled():
        return 0.0
    return _read_seg("HONGGUO_GOLDEN_OPEN_SEC", 4.0, lo=2.0, hi=6.0)


def ai_body_script_max_sec() -> float:
    """AI 剪辑脚本（正片 clips 合计）硬上限。默认 23s 强冲突快剪。"""
    return _read_seg("HONGGUO_AI_BODY_SCRIPT_MAX_SEC", 23.0, lo=15.0, hi=40.0)


def ai_body_script_optimal_range() -> tuple[float, float]:
    """AI 剪辑脚本推荐时长区间（秒）。默认聚焦 23s 冲突高光。"""
    lo = _read_seg("HONGGUO_AI_BODY_SCRIPT_OPT_LO", 21.0, lo=15.0, hi=30.0)
    hi = _read_seg("HONGGUO_AI_BODY_SCRIPT_OPT_HI", 23.0, lo=18.0, hi=35.0)
    mx = ai_body_script_max_sec()
    return lo, min(hi, mx)


def ai_script_duration_guidance() -> str:
    lo, hi = ai_body_script_optimal_range()
    mx = ai_body_script_max_sec()
    return (
        f"剪辑脚本（正片 body 的 clips 合计）**不得超过 {mx:.0f} 秒**，"
        f"**{lo:.0f}–{hi:.0f} 秒为最佳**。"
    )


def body_main_sec() -> float:
    """正片参考时长（默认取最佳区间中点）。"""
    try:
        from hook_duration_budget import (
            hook_target_total_sec,
            is_thirty_second_hook_preset,
        )

        if is_thirty_second_hook_preset():
            return max(8.0, hook_target_total_sec() - intro_outro_overhead_pro())
    except ImportError:
        pass
    lo, hi = ai_body_script_optimal_range()
    default_mid = (lo + hi) / 2.0
    return _read_seg("HONGGUO_BODY_MAIN_SEC", default_mid, lo=lo, hi=hi)


def freeze_hold_sec() -> float:
    """正片结尾过渡时长（淡出模式为淡出长度，定格模式为静帧时长）；0=跳过，直接接片尾口播。"""
    return _read_seg("HONGGUO_FREEZE_SEC", 0.0, lo=0.0, hi=12.0)


def body_outro_skip_transition() -> bool:
    """正片与片尾口播之间不插黑场/淡出/定格。"""
    v = os.getenv("HONGGUO_BODY_OUTRO_SKIP", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return freeze_hold_sec() <= 0.05


def body_tail_fade_sec() -> float:
    """
    正片最后一镜末尾淡出（秒）。
    跳过尾过渡段（BODY_OUTRO_SKIP）时默认仍淡出，避免硬切到片尾口播。
    """
    raw = os.getenv("HONGGUO_BODY_TAIL_FADE_SEC", "").strip()
    if raw:
        try:
            return max(0.0, min(2.5, float(raw)))
        except ValueError:
            pass
    if body_outro_skip_transition():
        return 1.0
    return 0.0


def body_outro_use_fade() -> bool:
    """正片结尾用淡出到黑（默认），替代硬定格。"""
    v = os.getenv("HONGGUO_BODY_OUTRO_FADE", "1").strip().lower()
    return v not in ("0", "false", "no", "off", "freeze")


def outro_tail_video_sec() -> float:
    """片尾过渡复用正片末端的实拍秒数；默认 0=黑场字幕，避免结局画面重复。"""
    try:
        return max(0.0, min(0.8, float(os.getenv("HONGGUO_OUTRO_TAIL_VIDEO_SEC", "0"))))
    except ValueError:
        return 0.0


def outro_cta_sec() -> float:
    if not outro_voiceover_enabled():
        return 0.0
    return _read_seg("HONGGUO_OUTRO_CTA_SEC", 3.0, lo=2.0, hi=6.0)


def timeline_total_sec() -> float:
    return golden_open_sec() + body_main_sec() + freeze_hold_sec() + outro_cta_sec()


def intro_outro_overhead_pro() -> float:
    """不含正片：黄金口播 + 尾过渡 + 尾帧。"""
    tail = 0.0 if body_outro_skip_transition() else freeze_hold_sec()
    return golden_open_sec() + tail + outro_cta_sec()


def fixed_opening_line() -> str:
    return (
        os.getenv("HONGGUO_FIXED_OPENING_TEXT", "").strip()
        or "30秒速览精品好剧！"
    )


def fixed_outro_line() -> str:
    return (
        os.getenv("HONGGUO_FIXED_OUTRO_TEXT", "").strip()
        or "关注我，带您看更多好剧！"
    )


def freeze_caption_text() -> str:
    """正片尾过渡字幕；默认空（不显示「后续剧情更精彩」等）。"""
    return os.getenv("HONGGUO_FREEZE_CAPTION", "").strip()


def opening_bgm_path() -> str:
    return os.getenv("HONGGUO_OPENING_BGM", "").strip()


def timeline_summary() -> str:
    """成片合成前的 .env 参考项（故事优先模式下非成片时长标准）。"""
    opt_lo, opt_hi = ai_body_script_optimal_range()
    story_note = (
        f"；{ai_script_duration_guidance()}"
        if story_first_edit_enabled()
        else f"；正片参考 {opt_lo:.0f}–{opt_hi:.0f}s"
    )
    return (
        f"专业30s 模板参考(.env)：黄金口播≤{golden_open_sec():.0f}s + "
        f"正片参考{body_main_sec():.0f}s + 尾过渡{freeze_hold_sec():.0f}s + "
        f"尾帧{outro_cta_sec():.0f}s{story_note}"
    )
