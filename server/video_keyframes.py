"""从正片抽取关键帧，供 Gemma 4 等多模态模型「看见」画面选 clip。"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from video_transcript import _ffmpeg

logger = logging.getLogger(__name__)


def vision_frames_enabled() -> bool:
    v = os.getenv("HONGGUO_VISION_FRAMES", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def vision_max_frames() -> int:
    try:
        return max(4, min(24, int(os.getenv("HONGGUO_VISION_MAX_FRAMES", "16"))))
    except ValueError:
        return 16


def vision_effective_max_for_api() -> int:
    """发给 LLM 的图片数，默认与 HONGGUO_VISION_MAX_FRAMES 一致（16）。"""
    cap = vision_max_frames()
    raw = os.getenv("HONGGUO_VISION_LM_MAX_FRAMES", "").strip()
    if not raw:
        return cap
    try:
        return min(cap, max(4, min(24, int(raw))))
    except ValueError:
        return cap


def _subsample_ordered(items: list, max_n: int) -> list:
    if len(items) <= max_n:
        return items
    if max_n <= 1:
        return items[:1]
    last = len(items) - 1
    return [items[int(round(i * last / (max_n - 1)))] for i in range(max_n)]


def vision_front_ratio() -> float:
    """优先在前段与高能轴选帧，避免 30~70s 等大空档看不见画面。"""
    try:
        return min(0.75, max(0.4, float(os.getenv("HONGGUO_VISION_FRONT_RATIO", "0.6"))))
    except ValueError:
        return 0.6


def vision_frame_width() -> int:
    try:
        return max(256, min(1024, int(os.getenv("HONGGUO_VISION_FRAME_WIDTH", "512"))))
    except ValueError:
        return 512


def _mandatory_keyframe_times(
    duration: float,
    *,
    visual_peaks: list[float],
    dialogue_peaks: list[float],
    first_dialogue_sec: float | None,
) -> list[float]:
    """覆盖无声开场、羞辱、虐点、集末等关键节点。"""
    dur = max(1.0, float(duration))
    mandatory = [0.9, min(10.0, dur * 0.07)]
    if visual_peaks:
        mandatory.append(visual_peaks[0])
    if first_dialogue_sec is not None and first_dialogue_sec > 5.0:
        mandatory.append(max(0.5, first_dialogue_sec))
        mandatory.append(min(dur - 1.0, first_dialogue_sec + 8.0))
    for t in dialogue_peaks[:4]:
        mandatory.append(t)
    mandatory.extend([dur * 0.5, dur * 0.78, max(0.5, dur - 3.0)])
    return [max(0.2, min(dur - 0.3, round(t, 2))) for t in mandatory]


def _pick_timestamps(
    duration: float,
    *,
    visual_peaks: list[float],
    dialogue_peaks: list[float],
    max_frames: int,
    first_dialogue_sec: float | None = None,
) -> list[float]:
    """合并必拍节点、画面高能、对白高能，再按上限截断。"""
    dur = max(1.0, float(duration))
    candidates: list[float] = _mandatory_keyframe_times(
        dur,
        visual_peaks=visual_peaks,
        dialogue_peaks=dialogue_peaks,
        first_dialogue_sec=first_dialogue_sec,
    )
    for t in visual_peaks:
        candidates.append(max(0.2, min(dur - 0.3, float(t))))
    for t in dialogue_peaks:
        candidates.append(max(0.2, min(dur - 0.3, float(t))))
    if not candidates:
        step = dur / (max_frames + 1)
        candidates = [step * (i + 1) for i in range(max_frames)]
    else:
        candidates.append(min(dur * 0.08, 2.0))
        candidates.append(dur * 0.5)
        candidates.append(dur * 0.85)

    candidates = sorted(set(round(t, 2) for t in candidates if 0 <= t < dur))
    if len(candidates) <= max_frames:
        return candidates

    front_cutoff = dur * vision_front_ratio()
    scored: list[tuple[float, float]] = []
    for t in candidates:
        score = 0.0
        if t <= front_cutoff:
            score += 3.0
        elif t <= dur * 0.75:
            score += 1.0
        if t <= min(12.0, dur * 0.08):
            score += 2.5
        if any(abs(t - v) < 1.5 for v in visual_peaks):
            score += 2.0
        if any(abs(t - v) < 2.0 for v in dialogue_peaks):
            score += 1.0
        if first_dialogue_sec is not None and abs(t - first_dialogue_sec) < 6.0:
            score += 1.5
        scored.append((score, t))

    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = sorted(t for _, t in scored[:max_frames])

    # 填补最大时间空档：在空档中点补 1 帧（替换得分最低且非前段必留的帧）
    max_gap = max(18.0, dur / max(max_frames, 1))
    for _ in range(3):
        if len(picked) >= max_frames:
            break
        worst_i, worst_gap = -1, 0.0
        for i in range(len(picked) - 1):
            gap = picked[i + 1] - picked[i]
            if gap > worst_gap:
                worst_gap, worst_i = gap, i
        if worst_gap < max_gap or worst_i < 0:
            break
        mid = round((picked[worst_i] + picked[worst_i + 1]) * 0.5, 2)
        if mid in picked or mid <= 0 or mid >= dur - 0.3:
            break
        drop_t = min(scored, key=lambda x: x[0])[1]
        if drop_t in picked and drop_t > front_cutoff:
            picked.remove(drop_t)
            picked.append(mid)
            picked.sort()
        else:
            picked.append(mid)
            picked.sort()
            if len(picked) > max_frames:
                picked = picked[:max_frames]

    return picked


def extract_frame_jpeg(video: Path, at_sec: float, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    w = vision_frame_width()
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{at_sec:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={w}:-2",
            "-q:v",
            "5",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 500


@dataclass
class KeyframeSample:
    at_sec: float
    path: Path
    caption: str

    def to_b64_url(self) -> str:
        raw = self.path.read_bytes()
        b64 = base64.standard_b64encode(raw).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"


def build_episode_keyframes(
    video: Path,
    work_dir: Path,
    *,
    duration: float,
    visual_moments: list | None = None,
    transcript_cues: list | None = None,
) -> list[KeyframeSample]:
    from video_moment_profile import VisualMoment
    from video_transcript import TranscriptCue
    from multi_clip import _HOOKY_KEYWORDS

    if not vision_frames_enabled():
        return []

    visual_peaks: list[float] = []
    for m in visual_moments or []:
        if isinstance(m, VisualMoment):
            visual_peaks.append((m.start_sec + m.end_sec) * 0.5)
        elif isinstance(m, dict):
            visual_peaks.append(
                (float(m.get("start_sec", 0)) + float(m.get("end_sec", 0))) * 0.5
            )

    dialogue_peaks: list[float] = []
    for c in transcript_cues or []:
        if isinstance(c, TranscriptCue):
            text, start = c.text, c.start_sec
        elif isinstance(c, dict):
            text, start = str(c.get("text", "")), float(c.get("start_sec", 0))
        else:
            continue
        if any(k in (text or "") for k in _HOOKY_KEYWORDS):
            dialogue_peaks.append(start)

    first_dialogue: float | None = None
    if transcript_cues:
        c0 = transcript_cues[0]
        first_dialogue = (
            float(c0.start_sec)
            if hasattr(c0, "start_sec")
            else float(c0.get("start_sec", 0))
        )
    times = _pick_timestamps(
        duration,
        visual_peaks=visual_peaks,
        dialogue_peaks=dialogue_peaks,
        max_frames=vision_max_frames(),
        first_dialogue_sec=first_dialogue,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    out: list[KeyframeSample] = []
    for i, t in enumerate(times):
        dest = work_dir / f"kf_{i:02d}_{t:.1f}s.jpg"
        if not extract_frame_jpeg(video, t, dest):
            continue
        out.append(
            KeyframeSample(
                at_sec=t,
                path=dest,
                caption=f"源片 t={t:.2f}s 关键帧（请根据画面动感/打斗/表情/特效判断是否入选正片）",
            )
        )
    if out:
        logger.info("已抽取 %d 张关键帧供视觉模型：%s", len(out), video.name)
    return out


def gather_episode_keyframes(
    *,
    series_id: str,
    episode_item_ids: list[str],
    episode_labels: list[str],
    episode_durations: list[float],
    work_dir: Path,
    episode_visual_profiles: dict | None = None,
    episode_transcripts: dict | None = None,
) -> dict[int, list[KeyframeSample]]:
    from fq_koc_material import find_local_material

    result: dict[int, list[KeyframeSample]] = {}
    for idx, item_id in enumerate(episode_item_ids, start=1):
        path = find_local_material(series_id, item_id)
        if not path:
            continue
        dur = (
            episode_durations[idx - 1]
            if idx - 1 < len(episode_durations)
            else 120.0
        )
        kfs = build_episode_keyframes(
            path,
            work_dir / f"keyframes_ep{idx:02d}",
            duration=dur,
            visual_moments=(episode_visual_profiles or {}).get(idx),
            transcript_cues=(episode_transcripts or {}).get(idx),
        )
        if kfs:
            result[idx] = kfs
    return result


def keyframes_to_message_content(
    keyframes: list[KeyframeSample],
    *,
    preamble: str,
) -> list[dict]:
    """OpenAI 兼容多模态 content 数组。"""
    parts: list[dict] = [{"type": "text", "text": preamble}]
    for kf in keyframes:
        parts.append({"type": "text", "text": kf.caption})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": kf.to_b64_url()},
            }
        )
    return parts


def build_edit_vision_user_content(
    episode_keyframes: dict[int, list[KeyframeSample]],
    episode_labels: list[str],
    *,
    task_prompt: str,
    max_images: int | None = None,
) -> list[dict]:
    """
    单条 user 消息内合并全部关键帧 + 剪辑任务（LM Studio 不接受连续多条带图 user）。
    """
    flat: list[tuple[int, str, KeyframeSample]] = []
    for idx in sorted(episode_keyframes.keys()):
        kfs = episode_keyframes[idx] or []
        if not kfs:
            continue
        label = (
            episode_labels[idx - 1]
            if idx - 1 < len(episode_labels)
            else f"第{idx}集"
        )
        for kf in kfs:
            flat.append((idx, label, kf))

    cap = max_images if max_images is not None else len(flat)
    if cap > 0 and len(flat) > cap:
        flat = _subsample_ordered(flat, cap)

    parts: list[dict] = [
        {
            "type": "text",
            "text": (
                f"【关键帧共 {len(flat)} 张】请结合画面与下方剪辑任务输出 JSON。\n"
                "观察打斗/表情/特效/构图，并与对白表、画面轴交叉选 clip。\n"
            ),
        }
    ]
    last_idx: int | None = None
    for ep_idx, label, kf in flat:
        if ep_idx != last_idx:
            n_ep = sum(1 for i, _, _ in flat if i == ep_idx)
            parts.append(
                {
                    "type": "text",
                    "text": vision_preamble_for_episode(label, n_ep),
                }
            )
            last_idx = ep_idx
        parts.append({"type": "text", "text": kf.caption})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": kf.to_b64_url()},
            }
        )
    parts.append(
        {
            "type": "text",
            "text": "\n\n=== 剪辑任务（只输出 JSON）===\n" + task_prompt,
        }
    )
    return parts


def vision_preamble_for_episode(label: str, n_frames: int) -> str:
    return (
        f"【{label} · 关键帧画面 {n_frames} 张】\n"
        "你是视觉模型：请直接观察每张图的动作、表情、打斗、特效、构图冲击力，"
        "与下方对白表/画面轴交叉后写出 clips。禁止只根据台词想象画面。\n"
    )
