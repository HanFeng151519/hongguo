"""多集钩子时长预算：支持 1 分钟专业推荐钩子（60±2s）与长钩子模式。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_edit_planner import BodySegmentPlan


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _read_sec_env(key: str, default: float, *, lo: float = 60.0, hi: float = 600.0) -> float:
    raw = os.getenv(key, str(default)).strip()
    try:
        v = float(raw)
    except ValueError:
        v = default
    return _clamp(v, lo, hi)


# 与 hook_generator 片头/片尾一致
_SPLASH_SEC = 1.0
_COMMENTARY_SEC = 1.2
_OUTRO_SEC = 2.0
_DEFAULT_TARGET_MIN = 150.0  # 2:30
_DEFAULT_TARGET_MAX = 210.0  # 3:30
_DEFAULT_TARGET_TOTAL = 180.0
_ONE_MIN_TARGET_MIN = 58.0
_ONE_MIN_TARGET_MAX = 62.0
_ONE_MIN_TARGET_TOTAL = 60.0
_ONE_MIN_COMPLETE_MIN = 54.0
_ONE_MIN_COMPLETE_MAX = 72.0
_ONE_MIN_COMPLETE_TOTAL = 62.0


def completeness_first_enabled() -> bool:
    try:
        from edge_tts_narration import dialogue_completeness_enabled

        return dialogue_completeness_enabled()
    except ImportError:
        return True


def _hook_preset() -> str:
    return os.getenv("HONGGUO_HOOK_PRESET", "").strip().lower()


def _preset_defaults() -> tuple[float, float, float]:
    """(min, max, target) 默认值。"""
    if _hook_preset() in ("1min", "one_minute", "one-minute", "一分钟", "60s", "60"):
        if completeness_first_enabled():
            return (
                _ONE_MIN_COMPLETE_MIN,
                _ONE_MIN_COMPLETE_MAX,
                _ONE_MIN_COMPLETE_TOTAL,
            )
        return _ONE_MIN_TARGET_MIN, _ONE_MIN_TARGET_MAX, _ONE_MIN_TARGET_TOTAL
    return _DEFAULT_TARGET_MIN, _DEFAULT_TARGET_MAX, _DEFAULT_TARGET_TOTAL


def hook_target_min_sec() -> float:
    """成片最短目标（含片头/解说/片尾）。"""
    env = os.getenv("HONGGUO_HOOK_MIN_SEC", "").strip()
    dmin, _, _ = _preset_defaults()
    return _read_sec_env("HONGGUO_HOOK_MIN_SEC", dmin) if env else dmin


def hook_target_max_sec() -> float:
    """成片最长目标（含片头/解说/片尾）。"""
    env = os.getenv("HONGGUO_HOOK_MAX_SEC", "").strip()
    _, dmax, _ = _preset_defaults()
    mx = _read_sec_env("HONGGUO_HOOK_MAX_SEC", dmax) if env else dmax
    mn = hook_target_min_sec()
    min_gap = 2.0 if _hook_preset() in ("1min", "one_minute", "one-minute", "一分钟", "60s", "60") else 15.0
    return max(mn + min_gap, mx)


def hook_target_total_sec() -> float:
    """偏好中点（用于文案/展示），落在 [min, max] 内。"""
    mn = hook_target_min_sec()
    mx = hook_target_max_sec()
    default_mid = (mn + mx) / 2.0
    _, _, dmid = _preset_defaults()
    raw = os.getenv("HONGGUO_HOOK_TARGET_SEC", str(dmid)).strip()
    try:
        v = float(raw)
    except ValueError:
        v = default_mid
    return _clamp(v, mn, mx)


def _fmt_mmss(sec: float) -> str:
    s = max(0, int(round(sec)))
    m, r = divmod(s, 60)
    if m > 0:
        return f"{m}分{r:02d}秒"
    return f"{r}秒"


def hook_duration_range_text() -> str:
    return f"{_fmt_mmss(hook_target_min_sec())}–{_fmt_mmss(hook_target_max_sec())}"


def is_one_minute_hook_preset() -> bool:
    return _hook_preset() in ("1min", "one_minute", "one-minute", "一分钟", "60s", "60")


def _use_pro_timeline() -> bool:
    try:
        from hook_timeline import pro_60_template_enabled

        return pro_60_template_enabled()
    except ImportError:
        return False


def opening_card_overhead_sec() -> float:
    """片头口播占用时长（用于预算）。"""
    if _use_pro_timeline():
        from hook_timeline import golden_open_sec

        return golden_open_sec()
    raw = os.getenv("HONGGUO_OPENING_CARD_SEC", "").strip()
    if raw:
        try:
            return max(1.0, min(10.0, float(raw)))
        except ValueError:
            pass
    try:
        from edge_tts_narration import fixed_opening_text

        if fixed_opening_text():
            return 4.2
    except ImportError:
        pass
    return _COMMENTARY_SEC


def hook_budget_enabled(episode_count: int) -> bool:
    """专业 60s：1 集起即按 46s 正片预算；多集长钩子默认 2 集起。"""
    if episode_count < 1:
        return False
    if _use_pro_timeline():
        return True
    if episode_count < 2:
        return False
    v = os.getenv("HONGGUO_HOOK_BUDGET", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def intro_outro_overhead_sec(*, with_commentary: bool = True) -> float:
    if _use_pro_timeline():
        from hook_timeline import intro_outro_overhead_pro

        return intro_outro_overhead_pro()
    total = _SPLASH_SEC + _OUTRO_SEC
    if with_commentary:
        total += opening_card_overhead_sec()
    return total


def body_budget_seconds(
    episode_count: int,
    *,
    with_commentary: bool = True,
    total_sec: float | None = None,
) -> float:
    """正片可用总秒数（不含片头/解说卡/片尾）。"""
    if episode_count < 1:
        return 0.0
    overhead = intro_outro_overhead_sec(with_commentary=with_commentary)
    if _use_pro_timeline():
        from hook_timeline import body_main_sec

        return body_main_sec()
    total = hook_target_max_sec() if total_sec is None else float(total_sec)
    body = total - overhead
    floor = 8.0 if is_one_minute_hook_preset() else 60.0
    return max(floor, body)


def per_episode_target_sec(
    episode_count: int,
    *,
    with_commentary: bool = True,
    total_sec: float | None = None,
) -> float:
    """单集正片目标；默认按上限分配，便于留足对白与转折。"""
    budget = body_budget_seconds(
        episode_count, with_commentary=with_commentary, total_sec=total_sec
    )
    return budget / max(1, episode_count)


def scale_body_segments_to_budget(
    segments: list[BodySegmentPlan],
    *,
    episode_count: int,
    with_commentary: bool = True,
) -> float:
    """把各集 duration_sec 缩放到 [min,max] 正片预算内；返回缩放后正片总时长。"""
    if not hook_budget_enabled(episode_count) or not segments:
        return sum(s.duration_sec for s in segments)

    budget_max = body_budget_seconds(
        episode_count, with_commentary=with_commentary, total_sec=hook_target_max_sec()
    )
    budget_min = body_budget_seconds(
        episode_count, with_commentary=with_commentary, total_sec=hook_target_min_sec()
    )
    n = len(segments)
    weights = [1.06] + [1.0] * (n - 1) if n > 1 else [1.0]
    wsum = sum(weights)
    # 按上限规划，减少为凑固定 3 分钟而过度压缩
    targets = [budget_max * w / wsum for w in weights]
    if _use_pro_timeline():
        min_each = max(3.0, budget_min / n * 0.38)
        # 专业 60s 单集需吃满整段正片预算（此前 28s 上限导致成片仅 ~44s）
        max_each = budget_max if n == 1 else min(28.0, budget_max / n * 1.6)
    elif is_one_minute_hook_preset():
        min_each = max(3.0, budget_min / n * 0.38)
        max_each = min(28.0, budget_max / n * 1.6)
    else:
        min_each = max(18.0, budget_min / n * 0.55)
        max_each = min(58.0, budget_max / n * 1.42)

    for seg, target in zip(segments, targets):
        seg.duration_sec = _clamp(float(seg.duration_sec or target), min_each, max_each)

    total = sum(s.duration_sec for s in segments)
    if total > budget_max * 1.02:
        ratio = budget_max / total
        for seg in segments:
            seg.duration_sec = _clamp(seg.duration_sec * ratio, min_each, max_each)
        total = sum(s.duration_sec for s in segments)
    elif total < budget_min * 0.98:
        ratio = budget_min / max(total, 0.01)
        for seg in segments:
            seg.duration_sec = _clamp(seg.duration_sec * ratio, min_each, max_each)
        total = sum(s.duration_sec for s in segments)
        if total < budget_min * 0.98:
            bump = (budget_min - total) / n
            for seg in segments:
                seg.duration_sec = _clamp(seg.duration_sec + bump, min_each, max_each)
            total = sum(s.duration_sec for s in segments)

    return total


def budget_summary(episode_count: int, *, with_commentary: bool = True) -> str:
    if not hook_budget_enabled(episode_count):
        return ""
    body_lo = body_budget_seconds(
        episode_count,
        with_commentary=with_commentary,
        total_sec=hook_target_min_sec(),
    )
    body_hi = body_budget_seconds(
        episode_count,
        with_commentary=with_commentary,
        total_sec=hook_target_max_sec(),
    )
    per_hi = per_episode_target_sec(
        episode_count, with_commentary=with_commentary, total_sec=hook_target_max_sec()
    )
    return (
        f"{episode_count} 集钩子预算：正片约 {body_lo:.0f}–{body_hi:.0f}s（约 {per_hi:.0f}s/集），"
        f"成片目标 {hook_duration_range_text()}"
    )
