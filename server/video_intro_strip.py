"""剪掉片头「网络视听」等备案初始画面（自动检测镜头切换点）。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def compliance_intro_strip_enabled() -> bool:
    v = os.getenv("HONGGUO_COMPLIANCE_INTRO_STRIP", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _ffmpeg() -> str:
    from ffmpeg_util import resolve_ffmpeg_exe

    return resolve_ffmpeg_exe()


def _intro_cache_path(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Path:
    from fq_koc_material import material_sidecar_stem

    stem = material_sidecar_stem(book_id, item_id) or video.stem
    return video.parent / f".{stem}_intro_skip.json"


def _load_intro_cache(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Optional[float]:
    path = _intro_cache_path(video, book_id=book_id, item_id=item_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if book_id and str(data.get("book_id") or "") not in ("", book_id):
            return None
        if item_id and str(data.get("item_id") or "") not in ("", item_id):
            return None
        if int(data.get("version") or 1) < 3:
            return None
        return max(0.0, float(data.get("skip_sec") or 0))
    except Exception as exc:
        logger.debug("读取 intro_skip 缓存失败: %s", exc)
        return None


def _save_intro_cache(
    video: Path,
    skip_sec: float,
    *,
    book_id: str = "",
    item_id: str = "",
    method: str = "scene",
) -> None:
    path = _intro_cache_path(video, book_id=book_id, item_id=item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "skip_sec": round(max(0.0, skip_sec), 3),
        "method": method,
        "version": 3,
        "book_id": book_id,
        "item_id": item_id,
        "video": video.name,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _scene_cuts_in_head(video: Path, max_sec: float) -> list[float]:
    """分析片头若干秒内的镜头切换时间。"""
    max_sec = max(2.0, float(max_sec))
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-t",
            f"{max_sec:.3f}",
            "-i",
            str(video),
            "-filter:v",
            "select='gt(scene,0.28)',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    times: list[float] = []
    for m in _PTS_RE.finditer(text):
        try:
            t = float(m.group(1))
        except ValueError:
            continue
        if 0.0 < t < max_sec:
            times.append(t)
    return sorted(set(times))


def detect_compliance_intro_skip_sec(video: Path) -> float:
    """
    检测片头备案卡结束位置（秒）。无备案画面时返回 0。
    网络视听等备案卡通常只在片头 2~4 秒，取该短窗口内最后一刀切镜，
    避免误跳到十几秒后正片。
    """
    if not video.is_file():
        return 0.0

    fixed = os.getenv("HONGGUO_COMPLIANCE_INTRO_FIXED_SEC", "").strip()
    if fixed:
        try:
            return max(0.0, float(fixed))
        except ValueError:
            pass

    try:
        max_scan = float(os.getenv("HONGGUO_COMPLIANCE_INTRO_MAX_SEC", "8"))
    except ValueError:
        max_scan = 8.0
    max_scan = max(4.0, min(12.0, max_scan))

    try:
        min_cut = float(os.getenv("HONGGUO_COMPLIANCE_INTRO_MIN_CUT_SEC", "2.0"))
    except ValueError:
        min_cut = 2.0

    try:
        max_cut = float(os.getenv("HONGGUO_COMPLIANCE_INTRO_MAX_CUT_SEC", "5.0"))
    except ValueError:
        max_cut = 5.0

    try:
        pad = float(os.getenv("HONGGUO_COMPLIANCE_INTRO_PAD_SEC", "0.15"))
    except ValueError:
        pad = 0.15

    cuts = _scene_cuts_in_head(video, max_scan)
    if not cuts:
        return 0.0

    # 备案区：片头前几秒内的切镜（常见 0.9 → 1.7 → 2.9 → 3.6）
    early = [t for t in cuts if t <= max_cut]
    if early:
        in_window = [t for t in early if min_cut <= t <= max_cut]
        if in_window:
            skip = max(in_window) + pad
        else:
            # 仅很早的切镜：取首个 >= min_cut
            for t in early:
                if t >= min_cut:
                    skip = t + pad
                    break
            else:
                skip = 0.0
        return min(max(skip, 0.0), max_cut + pad)

    for t in cuts:
        if min_cut <= t <= max_cut:
            return min(t + pad, max_cut + pad)

    return 0.0


def compliance_intro_skip_sec(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> float:
    """本片头应跳过的秒数（带缓存）。"""
    if not compliance_intro_strip_enabled() or not video.is_file():
        return 0.0

    cached = _load_intro_cache(video, book_id=book_id, item_id=item_id)
    if cached is not None:
        return cached

    skip = detect_compliance_intro_skip_sec(video)
    _save_intro_cache(
        video, skip, book_id=book_id, item_id=item_id, method="scene"
    )
    if skip > 0.05:
        logger.info(
            "片头备案画面跳过 %.2fs：%s (book=%s item=%s)",
            skip,
            video.name,
            book_id or "-",
            item_id or "-",
        )
    return skip


def offset_trim_start(
    trim_start_sec: float,
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> float:
    """在 AI/程序选段入点上叠加片头跳过。"""
    return max(0.0, float(trim_start_sec or 0)) + compliance_intro_skip_sec(
        video, book_id=book_id, item_id=item_id
    )


def shift_transcript_cues(cues: list, skip_sec: float) -> list:
    """对白时间轴减去片头备案时长。"""
    skip = max(0.0, float(skip_sec or 0))
    if skip < 0.05 or not cues:
        return cues
    from video_transcript import TranscriptCue

    out: list[TranscriptCue] = []
    for c in cues:
        if c.end_sec <= skip + 0.05:
            continue
        out.append(
            TranscriptCue(
                start_sec=max(0.0, c.start_sec - skip),
                end_sec=max(0.0, c.end_sec - skip),
                text=c.text,
            )
        )
    return out


def shift_visual_moments(moments: list, skip_sec: float) -> list:
    skip = max(0.0, float(skip_sec or 0))
    if skip < 0.05 or not moments:
        return moments
    from video_moment_profile import VisualMoment

    out: list[VisualMoment] = []
    for m in moments:
        if m.end_sec <= skip + 0.05:
            continue
        out.append(
            VisualMoment(
                start_sec=max(0.0, m.start_sec - skip),
                end_sec=max(0.0, m.end_sec - skip),
                tags=m.tags,
                score=m.score,
                note=m.note,
            )
        )
    return out
