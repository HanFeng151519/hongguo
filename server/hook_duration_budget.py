"""多集钩子时长预算：将多集正片压缩到约 3 分钟成片。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_edit_planner import BodySegmentPlan


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# 与 hook_generator 片头/片尾一致
_SPLASH_SEC = 1.0
_COMMENTARY_SEC = 1.2
_OUTRO_SEC = 2.0
_DEFAULT_TARGET_TOTAL = 180.0


def hook_target_total_sec() -> float:
    raw = os.getenv("HONGGUO_HOOK_TARGET_SEC", str(_DEFAULT_TARGET_TOTAL)).strip()
    try:
        v = float(raw)
    except ValueError:
        v = _DEFAULT_TARGET_TOTAL
    return max(90.0, min(600.0, v))


def hook_budget_enabled(episode_count: int) -> bool:
    """2 集及以上按总时长预算分配（默认整条约 3 分钟）。"""
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
) -> float:
    """正片可用总秒数（不含片头/解说卡/片尾）。"""
    if episode_count < 1:
        return 0.0
    overhead = intro_outro_overhead_sec(with_commentary=with_commentary)
    return max(60.0, hook_target_total_sec() - overhead)


def per_episode_target_sec(
    episode_count: int,
    *,
    with_commentary: bool = True,
) -> float:
    budget = body_budget_seconds(episode_count, with_commentary=with_commentary)
    return budget / max(1, episode_count)


def scale_body_segments_to_budget(
    segments: list[BodySegmentPlan],
    *,
    episode_count: int,
    with_commentary: bool = True,
) -> float:
    """把各集 duration_sec 缩放到预算内；返回缩放后正片总时长。"""
    if not hook_budget_enabled(episode_count) or not segments:
        return sum(s.duration_sec for s in segments)

    budget = body_budget_seconds(episode_count, with_commentary=with_commentary)
    n = len(segments)
    weights = [1.06] + [1.0] * (n - 1) if n > 1 else [1.0]
    wsum = sum(weights)
    targets = [budget * w / wsum for w in weights]
    min_each = max(16.0, budget / n * 0.5)
    max_each = min(52.0, budget / n * 1.35)

    for seg, target in zip(segments, targets):
        seg.duration_sec = _clamp(float(seg.duration_sec or target), min_each, max_each)

    total = sum(s.duration_sec for s in segments)
    if total > budget * 1.02:
        ratio = budget / total
        for seg in segments:
            seg.duration_sec = _clamp(seg.duration_sec * ratio, min_each, max_each)
        total = sum(s.duration_sec for s in segments)

    return total


def budget_summary(episode_count: int, *, with_commentary: bool = True) -> str:
    if not hook_budget_enabled(episode_count):
        return ""
    body = body_budget_seconds(episode_count, with_commentary=with_commentary)
    per = per_episode_target_sec(episode_count, with_commentary=with_commentary)
    return (
        f"{episode_count} 集钩子预算：正片约 {body:.0f}s（约 {per:.0f}s/集），"
        f"成片目标约 {hook_target_total_sec():.0f}s"
    )
