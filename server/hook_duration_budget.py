"""多集钩子时长预算：成片可在约 2分30秒–3分30秒 区间浮动，优先保证内容完整。"""

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


def hook_target_min_sec() -> float:
    """成片最短目标（含片头/解说/片尾）。"""
    return _read_sec_env("HONGGUO_HOOK_MIN_SEC", _DEFAULT_TARGET_MIN)


def hook_target_max_sec() -> float:
    """成片最长目标（含片头/解说/片尾）。"""
    mx = _read_sec_env("HONGGUO_HOOK_MAX_SEC", _DEFAULT_TARGET_MAX)
    mn = hook_target_min_sec()
    return max(mn + 15.0, mx)


def hook_target_total_sec() -> float:
    """偏好中点（用于文案/展示），落在 [min, max] 内。"""
    mn = hook_target_min_sec()
    mx = hook_target_max_sec()
    default_mid = (mn + mx) / 2.0
    raw = os.getenv("HONGGUO_HOOK_TARGET_SEC", str(_DEFAULT_TARGET_TOTAL)).strip()
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


def hook_budget_enabled(episode_count: int) -> bool:
    """2 集及以上按总时长预算分配。"""
    if episode_count < 2:
        return False
    v = os.getenv("HONGGUO_HOOK_BUDGET", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def intro_outro_overhead_sec(*, with_commentary: bool = True) -> float:
    total = _SPLASH_SEC + _OUTRO_SEC
    if with_commentary:
        total += _COMMENTARY_SEC
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
    total = hook_target_max_sec() if total_sec is None else float(total_sec)
    return max(60.0, total - overhead)


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
