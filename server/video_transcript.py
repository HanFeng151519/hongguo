"""从本地 MP4 抽取字幕或 ASR 对白时间轴，供 AI 分镜规划使用。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


@dataclass
class TranscriptCue:
    start_sec: float
    end_sec: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 2),
            "end_sec": round(self.end_sec, 2),
            "text": self.text,
        }


def asr_enabled() -> bool:
    v = os.getenv("HONGGUO_ASR_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def asr_model_name() -> str:
    return os.getenv("HONGGUO_ASR_MODEL", "small").strip() or "small"


def _ffmpeg() -> str:
    from ffmpeg_util import resolve_ffmpeg_exe

    return resolve_ffmpeg_exe()


def _srt_ts(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(content: str) -> list[TranscriptCue]:
    cues: list[TranscriptCue] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = _SRT_TIME.search(block)
        if not m:
            continue
        start = _srt_ts(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _srt_ts(m.group(5), m.group(6), m.group(7), m.group(8))
        text_lines = [
            ln
            for ln in lines
            if not ln.isdigit() and "-->" not in ln and not _SRT_TIME.search(ln)
        ]
        text = re.sub(r"\s+", " ", " ".join(text_lines)).strip()
        if text and end > start:
            cues.append(TranscriptCue(start, end, text))
    return cues


def _extract_embedded_subtitle(video: Path, work_dir: Path) -> list[TranscriptCue]:
    """尝试导出内嵌字幕轨为 SRT。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{video.stem}_subs.srt"
    out.unlink(missing_ok=True)
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-map",
            "0:s:0?",
            "-c:s",
            "srt",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 20:
        return []
    try:
        cues = parse_srt(out.read_text(encoding="utf-8", errors="replace"))
        if cues:
            logger.info("内嵌字幕 %d 条：%s", len(cues), video.name)
        return cues
    except OSError:
        return []


def _extract_audio_wav(video: Path, wav: Path) -> None:
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.unlink(missing_ok=True)
    proc = subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0 or not wav.is_file():
        tail = (proc.stderr or "")[-400:]
        raise RuntimeError(f"提取音频失败: {tail}")


def _apply_hf_endpoint() -> None:
    """国内下载 Whisper 权重易超时，可用 HONGGUO_HF_ENDPOINT 或 start.sh 设 HF_ENDPOINT。"""
    ep = (
        os.getenv("HONGGUO_HF_ENDPOINT", "").strip()
        or os.getenv("HF_ENDPOINT", "").strip()
    )
    if ep:
        os.environ["HF_ENDPOINT"] = ep


def _transcribe_faster_whisper(wav: Path) -> list[TranscriptCue]:
    from faster_whisper import WhisperModel

    _apply_hf_endpoint()
    device = os.getenv("HONGGUO_ASR_DEVICE", "cpu").strip() or "cpu"
    compute = os.getenv("HONGGUO_ASR_COMPUTE", "int8").strip() or "int8"
    model_id = asr_model_name()
    model_path = os.getenv("HONGGUO_ASR_MODEL_PATH", "").strip()
    cache_dir = os.getenv("HONGGUO_ASR_CACHE", "").strip() or None
    if model_path:
        model = WhisperModel(
            model_path, device=device, compute_type=compute, download_root=cache_dir
        )
    else:
        model = WhisperModel(
            model_id, device=device, compute_type=compute, download_root=cache_dir
        )
    segments, _info = model.transcribe(
        str(wav),
        language="zh",
        vad_filter=True,
        beam_size=int(os.getenv("HONGGUO_ASR_BEAM", "1") or 1),
    )
    cues: list[TranscriptCue] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if text:
            cues.append(TranscriptCue(float(seg.start), float(seg.end), text))
    return cues


def _transcribe_whisper_cli(wav: Path, work_dir: Path) -> list[TranscriptCue]:
    whisper = shutil.which("whisper")
    if not whisper:
        return []
    out_dir = work_dir / "whisper_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            whisper,
            str(wav),
            "--language",
            "Chinese",
            "--model",
            asr_model_name(),
            "--output_format",
            "json",
            "--output_dir",
            str(out_dir),
            "--fp16",
            "False",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        logger.warning("whisper CLI 失败: %s", (proc.stderr or "")[-300:])
        return []
    json_path = out_dir / f"{wav.stem}.json"
    if not json_path.is_file():
        return []
    data = json.loads(json_path.read_text(encoding="utf-8"))
    cues: list[TranscriptCue] = []
    for seg in data.get("segments") or []:
        text = str(seg.get("text") or "").strip()
        if text:
            cues.append(
                TranscriptCue(
                    float(seg.get("start") or 0),
                    float(seg.get("end") or 0),
                    text,
                )
            )
    return cues


def transcribe_video_audio(video: Path, work_dir: Path) -> list[TranscriptCue]:
    """ASR：优先 faster-whisper，其次 whisper CLI。"""
    wav = work_dir / f"{video.stem}_16k.wav"
    _extract_audio_wav(video, wav)
    engine = os.getenv("HONGGUO_ASR_ENGINE", "auto").strip().lower()
    if engine in ("faster", "faster_whisper", "faster-whisper"):
        return _transcribe_faster_whisper(wav)
    if engine == "whisper_cli":
        return _transcribe_whisper_cli(wav, work_dir)
    try:
        cues = _transcribe_faster_whisper(wav)
        if cues:
            logger.info("faster-whisper ASR %d 条：%s", len(cues), video.name)
            return cues
    except ImportError:
        logger.warning(
            "未安装 faster-whisper，尝试 whisper CLI；"
            "请执行: pip install -r server/requirements-asr.txt"
        )
    except Exception as exc:
        err = str(exc)
        if "timed out" in err.lower() or "hub" in err.lower():
            logger.warning(
                "faster-whisper 拉取模型失败（网络）：请在 .env 设 "
                "HONGGUO_HF_ENDPOINT=https://hf-mirror.com 或重启 ./start.sh（已默认镜像）"
            )
        logger.warning("faster-whisper 失败: %s", exc)
    cues = _transcribe_whisper_cli(wav, work_dir)
    if cues:
        logger.info("whisper CLI ASR %d 条：%s", len(cues), video.name)
        return cues
    if not shutil.which("whisper"):
        logger.error(
            "ASR 不可用：未安装 faster-whisper 且无 whisper 命令。"
            "请 pip install -r server/requirements-asr.txt 后重启 ./start.sh"
        )
    else:
        logger.error("ASR 失败：whisper CLI 未产出字幕，请查看上方 whisper 报错")
    return []


def transcript_cache_path(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Path:
    from fq_koc_material import material_sidecar_stem

    stem = material_sidecar_stem(book_id, item_id) or video.stem
    return video.parent / f".{stem}_transcript.json"


def _transcript_cache_ok(
    raw: dict,
    *,
    book_id: str,
    item_id: str,
) -> bool:
    if book_id and raw.get("book_id") and str(raw["book_id"]) != book_id:
        return False
    if item_id and raw.get("item_id") and str(raw["item_id"]) != item_id:
        return False
    return True


def _load_transcript_cache_file(path: Path, *, book_id: str, item_id: str) -> Optional[list[TranscriptCue]]:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and not _transcript_cache_ok(raw, book_id=book_id, item_id=item_id):
            return None
        items = raw.get("cues") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return None
        cues = [
            TranscriptCue(
                float(x["start_sec"]),
                float(x["end_sec"]),
                str(x["text"]),
            )
            for x in items
            if isinstance(x, dict) and x.get("text")
        ]
        return cues or None
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def load_transcript_cache(
    video: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> Optional[list[TranscriptCue]]:
    path = transcript_cache_path(video, book_id=book_id, item_id=item_id)
    cues = _load_transcript_cache_file(path, book_id=book_id, item_id=item_id)
    if cues:
        from video_intro_strip import (
            compliance_intro_strip_enabled,
            compliance_intro_skip_sec,
            shift_transcript_cues,
        )

        if compliance_intro_strip_enabled():
            skip = compliance_intro_skip_sec(
                video, book_id=book_id, item_id=item_id
            )
            cues = shift_transcript_cues(cues, skip)
        return cues
    if book_id and item_id:
        legacy = video.parent / f".{video.stem}_transcript.json"
        if legacy != path:
            cues = _load_transcript_cache_file(legacy, book_id=book_id, item_id=item_id)
            if cues:
                save_transcript_cache(video, cues, book_id=book_id, item_id=item_id)
    return None


def save_transcript_cache(
    video: Path,
    cues: list[TranscriptCue],
    *,
    book_id: str = "",
    item_id: str = "",
) -> None:
    path = transcript_cache_path(video, book_id=book_id, item_id=item_id)
    payload: dict = {
        "source": video.name,
        "cues": [c.to_dict() for c in cues],
    }
    if book_id:
        payload["book_id"] = book_id
    if item_id:
        payload["item_id"] = item_id
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def build_transcript_for_video(
    video: Path,
    work_dir: Path,
    *,
    book_id: str = "",
    item_id: str = "",
) -> list[TranscriptCue]:
    """字幕优先，其次 ASR；结果写入素材旁缓存（按 book_id+item_id 区分）。"""
    cached = load_transcript_cache(video, book_id=book_id, item_id=item_id)
    if cached:
        logger.info(
            "使用对白缓存 %d 条：%s (book=%s item=%s)",
            len(cached),
            video.name,
            book_id or "-",
            item_id or "-",
        )
        return cached

    cues = _extract_embedded_subtitle(video, work_dir)
    if not cues and asr_enabled():
        try:
            cues = transcribe_video_audio(video, work_dir)
        except Exception as exc:
            logger.warning("ASR 失败 %s: %s", video.name, exc)
            cues = []

    if cues:
        save_transcript_cache(video, cues, book_id=book_id, item_id=item_id)

    from video_intro_strip import (
        compliance_intro_strip_enabled,
        compliance_intro_skip_sec,
        shift_transcript_cues,
    )

    if cues and compliance_intro_strip_enabled():
        skip = compliance_intro_skip_sec(video, book_id=book_id, item_id=item_id)
        return shift_transcript_cues(cues, skip)
    return cues


def gather_episode_transcripts(
    *,
    series_id: str,
    episode_item_ids: list[str],
    episode_labels: list[str],
    work_dir: Path,
) -> dict[int, list[TranscriptCue]]:
    """按 episode_index（1-based）收集各集对白时间轴。"""
    from fq_koc_material import find_local_material

    out: dict[int, list[TranscriptCue]] = {}
    for idx, item_id in enumerate(episode_item_ids, start=1):
        label = episode_labels[idx - 1] if idx - 1 < len(episode_labels) else f"第{idx}集"
        path = find_local_material(series_id, item_id)
        if not path:
            logger.info(
                "%s 无本地 MP4（%s_%s.mp4），跳过对白抽取",
                label,
                series_id,
                item_id,
            )
            continue
        ep_work = work_dir / f"transcript_ep{idx:02d}"
        cues = build_transcript_for_video(
            path, ep_work, book_id=series_id, item_id=item_id
        )
        if cues:
            out[idx] = cues
            logger.info("%s 对白时间轴 %d 条", label, len(cues))
    return out


def transcript_full_for_ai() -> bool:
    v = os.getenv("HONGGUO_TRANSCRIPT_FULL", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def transcript_max_chars() -> int:
    raw = os.getenv("HONGGUO_TRANSCRIPT_MAX_CHARS", "120000").strip()
    try:
        return max(8000, int(raw))
    except ValueError:
        return 120000


_TRANSCRIPT_TABLE_HEADER = (
    "字段说明：每行一句对白；start_sec=该句起始秒，end_sec=该句结束秒（均为源片时间轴）。\n"
    "格式：#序号 | start_sec=起始秒 | end_sec=结束秒 | 台词\n"
    "---"
)


def format_cue_line(index: int, cue: TranscriptCue) -> str:
    """单行对白：显式标注起始秒与结束秒，供 LLM 精确选点。"""
    text = (cue.text or "").strip()[:200]
    start = round(float(cue.start_sec), 2)
    end = round(float(cue.end_sec), 2)
    dur = max(0.0, end - start)
    return (
        f"#{index:03d} | start_sec={start:.2f} | end_sec={end:.2f} "
        f"| duration_sec={dur:.2f} | {text}"
    )


def format_transcript_for_prompt(
    cues: list[TranscriptCue],
    *,
    max_lines: int = 80,
    max_chars: int | None = None,
) -> str:
    """LLM 可读的全量或抽样对白表：每行显式 start_sec / end_sec。"""
    if not cues:
        return "（无对白时间轴）"
    limit = transcript_max_chars() if max_chars is None else max_chars
    full = transcript_full_for_ai()
    lines: list[str] = [_TRANSCRIPT_TABLE_HEADER]
    total = len(_TRANSCRIPT_TABLE_HEADER) + 1
    step = 1
    if not full and len(cues) > max_lines:
        step = max(1, len(cues) // max_lines)

    line_no = 0
    for i, c in enumerate(cues):
        if not full and i % step != 0 and i != len(cues) - 1:
            continue
        line_no += 1
        line = format_cue_line(line_no, c)
        if total + len(line) + 1 > limit:
            omitted = len(cues) - i
            lines.append(
                f"…（已省略后续 {omitted} 条，请在前文 start_sec/end_sec 范围内选点；"
                f"禁止选用省略段之外的秒数）"
            )
            logger.warning("对白表超长，已截断至约 %d 字（共 %d 条）", total, line_no)
            break
        lines.append(line)
        total += len(line) + 1

    mode = "完整" if full and step == 1 else "抽样"
    header = f"共 {len(cues)} 条对白（{mode}列表，每句均含起始秒 start_sec 与结束秒 end_sec）：\n"
    return header + "\n".join(lines)


def write_transcript_sidecar(
    cues: list[TranscriptCue],
    path: Path,
) -> None:
    """写入完整对白文本，便于核对 AI 输入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_TRANSCRIPT_TABLE_HEADER]
    n = 0
    for c in cues:
        if not (c.text or "").strip():
            continue
        n += 1
        lines.append(format_cue_line(n, c))
    path.write_text("\n".join(lines), encoding="utf-8")


def find_cue_at(cues: list[TranscriptCue], sec: float) -> TranscriptCue | None:
    for c in cues:
        if c.start_sec - 0.5 <= sec <= c.end_sec + 0.5:
            return c
        if c.start_sec > sec + 2.0:
            break
    if not cues:
        return None
    best = min(cues, key=lambda c: abs(c.start_sec - sec))
    return best if abs(best.start_sec - sec) <= 8.0 else None


def _align_clips_build(
    clips: list,
    cues: list[TranscriptCue],
) -> list:
    """按对白起止拉长 clip，不做时长压缩。"""
    from multi_clip import ClipFragment

    out: list[ClipFragment] = []
    for c in sorted(clips, key=lambda x: x.trim_start_sec):
        anchor = find_cue_at(cues, float(c.trim_start_sec))
        if not anchor:
            out.append(c)
            continue
        from edge_tts_narration import dialogue_completeness_enabled, dialogue_tail_pad_sec

        pad = dialogue_tail_pad_sec() if dialogue_completeness_enabled() else 0.25
        start = max(0.0, anchor.start_sec - 0.35)
        end = anchor.end_sec + pad
        for cue in cues:
            if cue.start_sec < anchor.start_sec - 0.1:
                continue
            if cue.start_sec > anchor.end_sec + 18.0:
                break
            if cue.start_sec <= anchor.end_sec + 0.15:
                end = max(end, cue.end_sec + pad)
            elif dialogue_completeness_enabled() and cue.start_sec < end + 8.0:
                if _reason_is_hooky(cue.text) or (cue.end_sec - cue.start_sec) > 1.2:
                    end = max(end, cue.end_sec + pad)
        from multi_clip import clip_min_sec

        dur = max(clip_min_sec(), end - start)
        reason = c.reason or ""
        if "start_sec" not in reason and "[" not in reason:
            reason = (
                f"{reason} start_sec={anchor.start_sec:.2f} "
                f"end_sec={anchor.end_sec:.2f}"
            )
        out.append(
            ClipFragment(
                trim_start_sec=start,
                duration_sec=dur,
                reason=reason,
            )
        )

    return out


def _log_dialogue_align_total(
    out: list, *, target_sec: float, compressed: bool, before: float | None = None
) -> None:
    from edge_tts_narration import dialogue_compress_grace_sec

    total = sum(x.duration_sec for x in out)
    if compressed and before is not None:
        logger.info(
            "对白完整优先：正片 %.1fs→%.1fs（超出目标 %.0fs 超过容差 %.0fs，已压缩）",
            before,
            total,
            target_sec,
            dialogue_compress_grace_sec(),
        )
        return
    over = total - target_sec
    if over > 0.5:
        logger.info(
            "对白完整优先：正片 %.1fs（目标 %.0fs，多 %.1fs≤容差 %.0fs，未压缩台词）",
            total,
            target_sec,
            over,
            dialogue_compress_grace_sec(),
        )
    else:
        logger.info(
            "对白完整优先：正片 %.1fs（未截断台词，目标约 %.0fs）",
            total,
            target_sec,
        )


def align_clips_to_transcript(
    clips: list,
    cues: list[TranscriptCue],
    *,
    target_sec: float,
    compress: bool | None = None,
) -> list:
    """将 AI clips 贴到真实对白起止；compress=False 时不压时长。"""
    if compress is None:
        from edge_tts_narration import dialogue_compress_enabled

        compress = dialogue_compress_enabled()
    if not clips or not cues:
        return clips

    out = _align_clips_build(clips, cues)
    from multi_clip import enforce_clip_duration_floor

    src_end = max((c.end_sec for c in cues), default=target_sec) + 2.0
    out = enforce_clip_duration_floor(out, src_end, cues=cues)

    from edge_tts_narration import dialogue_completeness_enabled

    if dialogue_completeness_enabled():
        total = sum(x.duration_sec for x in out)
        from edge_tts_narration import should_compress_for_dialogue

        if compress and should_compress_for_dialogue(total, target_sec):
            from multi_clip import compress_clips_abc_priority

            before = total
            out = compress_clips_abc_priority(out, target_sec)
            out = enforce_clip_duration_floor(out, src_end, cues=cues)
            _log_dialogue_align_total(out, target_sec=target_sec, compressed=True, before=before)
        else:
            _log_dialogue_align_total(out, target_sec=target_sec, compressed=False)
    elif not dialogue_completeness_enabled():
        total = sum(x.duration_sec for x in out)
        if total > 1.0 and abs(total - target_sec) > 1.5:
            ratio = target_sec / total
            from multi_clip import clip_min_sec

            for x in out:
                x.duration_sec = max(
                    clip_min_sec(), min(18.0, x.duration_sec * ratio)
                )
    return out


def align_clips_dialogue_variants(
    clips: list,
    cues: list[TranscriptCue],
    *,
    target_sec: float,
) -> tuple[list, list | None]:
    """
    返回 (主版本 clips, 未压缩完整版或 None)。
    仅当完整版与压缩版时长差异明显时提供第二份。
    """
    full = align_clips_to_transcript(clips, cues, target_sec=target_sec, compress=False)
    primary = align_clips_to_transcript(clips, cues, target_sec=target_sec, compress=True)
    from edge_tts_narration import dialogue_dual_min_gap_sec

    full_total = sum(c.duration_sec for c in full)
    pri_total = sum(c.duration_sec for c in primary)
    gap = full_total - pri_total
    min_gap = dialogue_dual_min_gap_sec()
    if gap < min_gap:
        logger.info(
            "对白双版本：未压缩 %.1fs / 压缩 %.1fs（相差 %.1fs < %.0fs，仅导出压缩版）",
            full_total,
            pri_total,
            gap,
            min_gap,
        )
        return primary, None
    logger.info(
        "对白双版本：未压缩 %.1fs / 压缩 %.1fs（相差 %.1fs，将各导出一条成片）",
        full_total,
        pri_total,
        gap,
    )
    return primary, full


def _reason_is_hooky(text: str) -> bool:
    from multi_clip import _HOOKY_KEYWORDS

    t = (text or "").strip()
    return any(k in t for k in _HOOKY_KEYWORDS)


def transcript_block_for_episodes(
    episode_transcripts: dict[int, list[TranscriptCue]],
    episode_labels: list[str],
    *,
    body_target_sec: float = 50.0,
) -> str:
    if not episode_transcripts:
        return ""
    parts: list[str] = []
    for idx in sorted(episode_transcripts.keys()):
        label = episode_labels[idx - 1] if idx - 1 < len(episode_labels) else f"第{idx}集"
        body = format_transcript_for_prompt(episode_transcripts[idx])
        parts.append(
            f"### {label} 完整对白时间轴（你只能从下表选点，禁止编造）\n{body}"
        )
    return (
        "\n\n【对白时间轴 · 情节与金句线索，不能单独据此剪片】\n"
        "须与下方「画面/音效高能轴」交叉：有打斗/快切/音效峰的段落优先于纯对白铺垫。\n"
        + "\n\n".join(parts)
        + "\n\n对白表使用规则：\n"
        f"1. 正片总长约 {body_target_sec:.0f}s；clips.duration_sec 之和须等于 body duration_sec（±0.5）。\n"
        "2. trim_start_sec 对齐某句 start_sec；duration 覆盖该句 end_sec（可含下一句）。\n"
        "3. reason 写 A|/B| + start_sec/end_sec；无对白的高能画面可只引用画面轴秒数。\n"
    )


def transcript_edit_required() -> bool:
    v = os.getenv("HONGGUO_AI_REQUIRE_TRANSCRIPT", "1").strip().lower()
    return v not in ("0", "false", "no", "off")
