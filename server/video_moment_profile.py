"""从 MP4 抽取画面动感、镜头切换、音浪高峰，供 AI 分镜（弥补纯文本 LLM 看不见画面）。"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import subprocess
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from video_transcript import _extract_audio_wav, _ffmpeg

logger = logging.getLogger(__name__)

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


@dataclass
class VisualMoment:
    start_sec: float
    end_sec: float
    tags: str
    score: float
    note: str

    def to_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 2),
            "end_sec": round(self.end_sec, 2),
            "tags": self.tags,
            "score": round(self.score, 2),
            "note": self.note,
        }


def visual_profile_enabled() -> bool:
    v = os.getenv("HONGGUO_VISUAL_PROFILE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _profile_cache_path(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Path:
    from fq_koc_material import material_sidecar_stem

    stem = material_sidecar_stem(book_id, item_id) or video.stem
    return video.parent / f".{stem}_visual_profile.json"


def _visual_cache_ok(raw: dict, *, book_id: str, item_id: str) -> bool:
    if book_id and raw.get("book_id") and str(raw["book_id"]) != book_id:
        return False
    if item_id and raw.get("item_id") and str(raw["item_id"]) != item_id:
        return False
    return True


def _load_cache_file(
    path: Path,
    *,
    book_id: str,
    item_id: str,
) -> Optional[list[VisualMoment]]:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and not _visual_cache_ok(raw, book_id=book_id, item_id=item_id):
            return None
        items = raw.get("moments") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return None
        out: list[VisualMoment] = []
        for x in items:
            if not isinstance(x, dict):
                continue
            out.append(
                VisualMoment(
                    float(x["start_sec"]),
                    float(x["end_sec"]),
                    str(x.get("tags") or ""),
                    float(x.get("score") or 0),
                    str(x.get("note") or ""),
                )
            )
        return out or None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _load_cache(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Optional[list[VisualMoment]]:
    path = _profile_cache_path(video, book_id=book_id, item_id=item_id)
    moments = _load_cache_file(path, book_id=book_id, item_id=item_id)
    if moments:
        return moments
    if book_id and item_id:
        legacy = video.parent / f".{video.stem}_visual_profile.json"
        if legacy != path:
            moments = _load_cache_file(legacy, book_id=book_id, item_id=item_id)
            if moments:
                _save_cache(video, moments, book_id=book_id, item_id=item_id)
    return None


def _save_cache(
    video: Path,
    moments: list[VisualMoment],
    *,
    book_id: str = "",
    item_id: str = "",
) -> None:
    path = _profile_cache_path(video, book_id=book_id, item_id=item_id)
    payload: dict = {
        "source": video.name,
        "moments": [m.to_dict() for m in moments],
    }
    if book_id:
        payload["book_id"] = book_id
    if item_id:
        payload["item_id"] = item_id
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def _audio_rms_windows(wav: Path, *, window_sec: float = 0.25) -> list[tuple[float, float]]:
    """返回 (时间中点秒, RMS) 序列。"""
    with wave.open(str(wav), "rb") as wf:
        rate = wf.getframerate()
        width = wf.getsampwidth()
        if width != 2:
            raise ValueError(f"仅支持 16-bit PCM: {wav}")
        frames_per_win = max(1, int(rate * window_sec))
        fmt = f"<{frames_per_win * width // width}h"
        t = 0.0
        out: list[tuple[float, float]] = []
        while True:
            raw = wf.readframes(frames_per_win)
            if not raw:
                break
            n = len(raw) // width
            if n < 1:
                break
            samples = struct.unpack(f"<{n}h", raw[: n * width])
            mean_sq = sum(s * s for s in samples) / max(1, len(samples))
            rms = (mean_sq**0.5) / 32768.0
            mid = t + window_sec * 0.5
            out.append((mid, rms))
            t += window_sec
    return out


def _peaks_to_segments(
    windows: list[tuple[float, float]],
    *,
    percentile: float = 82.0,
    min_dur: float = 0.8,
    max_dur: float = 10.0,
    gap: float = 0.6,
) -> list[tuple[float, float, float]]:
    """高能音频段：(start, end, peak_score)。"""
    if not windows:
        return []
    vals = sorted(v for _, v in windows)
    idx = int(min(len(vals) - 1, max(0, len(vals) * percentile / 100.0)))
    thresh = max(0.02, vals[idx])
    segs: list[tuple[float, float, float]] = []
    cur_start: Optional[float] = None
    cur_end = 0.0
    peak = 0.0
    for t, rms in windows:
        if rms >= thresh:
            if cur_start is None:
                cur_start = max(0.0, t - 0.25)
            cur_end = t + 0.25
            peak = max(peak, rms)
        elif cur_start is not None and t - cur_end > gap:
            dur = cur_end - cur_start
            if dur >= min_dur:
                segs.append((cur_start, min(cur_end, cur_start + max_dur), peak))
            cur_start = None
            peak = 0.0
    if cur_start is not None:
        dur = cur_end - cur_start
        if dur >= min_dur:
            segs.append((cur_start, min(cur_end, cur_start + max_dur), peak))
    return segs


def _scene_cut_times(video: Path) -> list[float]:
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-hide_banner",
            "-i",
            str(video),
            "-filter:v",
            "select='gt(scene,0.32)',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    times: list[float] = []
    for m in _PTS_RE.finditer(text):
        try:
            times.append(float(m.group(1)))
        except ValueError:
            continue
    return sorted(set(times))


def _cuts_near(t: float, cuts: list[float], radius: float = 2.5) -> int:
    lo, hi = t - radius, t + radius
    return sum(1 for c in cuts if lo <= c <= hi)


def build_visual_profile(
    video: Path,
    work_dir: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> list[VisualMoment]:
    cached = _load_cache(video, book_id=book_id, item_id=item_id)
    if cached:
        logger.info(
            "使用画面/音效缓存 %d 段：%s (book=%s item=%s)",
            len(cached),
            video.name,
            book_id or "-",
            item_id or "-",
        )
        return cached

    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / f"{video.stem}_profile_16k.wav"
    try:
        _extract_audio_wav(video, wav)
        windows = _audio_rms_windows(wav)
        audio_segs = _peaks_to_segments(windows)
    except Exception as exc:
        logger.warning("音轨分析失败 %s: %s", video.name, exc)
        audio_segs = []

    try:
        cuts = _scene_cut_times(video)
    except Exception as exc:
        logger.warning("镜头切分分析失败 %s: %s", video.name, exc)
        cuts = []

    moments: list[VisualMoment] = []
    for start, end, peak in audio_segs:
        ncut = _cuts_near((start + end) * 0.5, cuts)
        tags: list[str] = ["sfx_high"]
        note_parts = ["音浪高峰/冲击音效"]
        if ncut >= 2:
            tags.extend(["motion", "fight"])
            note_parts.append(f"镜头快切×{ncut}")
        elif ncut == 1:
            tags.append("motion")
            note_parts.append("有切镜")
        score = min(1.0, peak * 4.0 + ncut * 0.12)
        moments.append(
            VisualMoment(
                start,
                end,
                tags="+".join(tags),
                score=score,
                note="；".join(note_parts),
            )
        )

    # 纯快切但音浪一般的段落（连贯动作戏）
    for i, ct in enumerate(cuts):
        if any(abs(m.start_sec - ct) < 3.0 for m in moments):
            continue
        n_near = _cuts_near(ct, cuts, radius=4.0)
        if n_near >= 3:
            start = max(0.0, ct - 1.0)
            end = ct + min(8.0, 2.0 + n_near * 0.8)
            moments.append(
                VisualMoment(
                    start,
                    end,
                    tags="motion+scene_cuts",
                    score=0.45 + n_near * 0.08,
                    note=f"连续快切×{n_near}（画面动感强）",
                )
            )

    def _merge_tag_str(a: str, b: str) -> str:
        parts: set[str] = set()
        for raw in (a, b):
            for t in raw.replace("|", "+").split("+"):
                t = t.strip()
                if t:
                    parts.add(t)
        return "+".join(sorted(parts))

    moments.sort(key=lambda m: m.start_sec)
    merged: list[VisualMoment] = []
    for m in moments:
        if merged and m.start_sec <= merged[-1].end_sec + 0.5:
            prev = merged[-1]
            merged[-1] = VisualMoment(
                prev.start_sec,
                max(prev.end_sec, m.end_sec),
                tags=_merge_tag_str(prev.tags, m.tags),
                score=max(prev.score, m.score),
                note=f"{prev.note}；{m.note}"[:120],
            )
        else:
            merged.append(m)

    if merged:
        _save_cache(video, merged, book_id=book_id, item_id=item_id)
        logger.info("画面/音效轴 %d 段：%s", len(merged), video.name)
    return merged


def format_moment_line(index: int, m: VisualMoment) -> str:
    return (
        f"#{index:03d} | start_sec={m.start_sec:.2f} | end_sec={m.end_sec:.2f} "
        f"| tags={m.tags} | score={m.score:.2f} | {m.note}"
    )


_VISUAL_TABLE_HEADER = (
    "字段：系统从音轨+镜头切分推断的高能画面（非对白）。tags: sfx_high=音效冲击, "
    "motion=动感/快切, fight=打斗倾向。\n"
    "格式：#序号 | start_sec | end_sec | tags | score | 说明\n"
    "---"
)


def format_visual_profile_for_prompt(moments: list[VisualMoment]) -> str:
    if not moments:
        return "（未检测到明显打斗/音效高峰，请结合对白表与剧情常识选动感镜头）"
    lines = [_VISUAL_TABLE_HEADER]
    for i, m in enumerate(moments, start=1):
        lines.append(format_moment_line(i, m))
    return (
        f"共 {len(moments)} 段高能画面/音效（完整列表）：\n" + "\n".join(lines)
    )


def visual_block_for_episodes(
    episode_profiles: dict[int, list[VisualMoment]],
    episode_labels: list[str],
) -> str:
    if not episode_profiles:
        return ""
    parts: list[str] = []
    for idx in sorted(episode_profiles.keys()):
        label = episode_labels[idx - 1] if idx - 1 < len(episode_labels) else f"第{idx}集"
        body = format_visual_profile_for_prompt(episode_profiles[idx])
        parts.append(f"### {label} 画面/音效高能轴\n{body}")
    return (
        "\n\n【画面与音效时间轴 · 找「最有冲击力」的镜头】\n"
        "score 越高越适合留住观众；tags=fight/motion/sfx_high 优先入选正片。"
        "与本表 + 对白表交叉选 clip：先保证前几秒有冲击，再保证剧情听得懂。\n"
        + "\n\n".join(parts)
    )


def gather_episode_visual_profiles(
    *,
    series_id: str,
    episode_item_ids: list[str],
    episode_labels: list[str],
    work_dir: Path,
) -> dict[int, list[VisualMoment]]:
    from fq_koc_material import find_local_material

    if not visual_profile_enabled():
        return {}
    out: dict[int, list[VisualMoment]] = {}
    for idx, item_id in enumerate(episode_item_ids, start=1):
        label = episode_labels[idx - 1] if idx - 1 < len(episode_labels) else f"第{idx}集"
        path = find_local_material(series_id, item_id)
        if not path:
            continue
        ep_work = work_dir / f"visual_ep{idx:02d}"
        moments = build_visual_profile(
            path, ep_work, book_id=series_id, item_id=item_id
        )
        if moments:
            out[idx] = moments
            logger.info("%s 画面/音效轴 %d 段", label, len(moments))
    return out
