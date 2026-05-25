"""Edge TTS 解说音轨（晓晓 / 晓伊 / 云阳等 Neural 音色）。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")
_TTS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="edge_tts")

logger = logging.getLogger(__name__)

# 常用中文 Neural 音色（edge-tts voice 名）
VOICE_PRESETS: dict[str, str] = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "晓伊": "zh-CN-XiaoyiNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "云阳": "zh-CN-YunyangNeural",
}
DEFAULT_VOICE_KEY = "xiaoyi"
DEFAULT_RATE = "+2%"
DEFAULT_VOLUME = "+0%"
DEFAULT_PITCH = "+0Hz"
DEFAULT_ORIGINAL_VOLUME = 1.06
DEFAULT_DUCK_DURING_NARR = 0.58
DEFAULT_NARR_VOLUME = 0.92


def tts_enabled() -> bool:
    v = os.getenv("HONGGUO_TTS", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def tts_on_body_enabled() -> bool:
    """正片是否混解说配音（原味模式默认关，保留对白原声）。"""
    if not tts_enabled():
        return False
    v = os.getenv("HONGGUO_TTS_ON_BODY", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    try:
        from video_originality import authentic_preservation_enabled

        return not authentic_preservation_enabled()
    except ImportError:
        return True


def edge_tts_available() -> bool:
    try:
        import edge_tts  # noqa: F401

        return True
    except ImportError:
        return False


def resolve_voice(voice_key: Optional[str] = None) -> str:
    """环境变量 HONGGUO_TTS_VOICE：xiaoxiao | xiaoyi | yunyang | random"""
    raw = (voice_key or os.getenv("HONGGUO_TTS_VOICE", DEFAULT_VOICE_KEY)).strip().lower()
    if raw in ("random", "rand", "auto"):
        return random.choice(list(VOICE_PRESETS.values()))
    return VOICE_PRESETS.get(raw, VOICE_PRESETS[DEFAULT_VOICE_KEY])


def clean_title_for_hint(title: str, *, max_len: int = 28) -> str:
    """剧名用于《》内展示，去掉已有书名号。"""
    t = re.sub(r"\s+", " ", (title or "").strip()).strip("《》「」【】")
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t or "短剧"


def search_hint_phrase(title: str) -> str:
    """首页语音：请搜索《剧名》在红果短剧观看原片"""
    short = clean_title_for_hint(title)
    return f"请搜索《{short}》在红果短剧观看原片"


def _run_coro_sync(coro: Coroutine[Any, Any, T]) -> T:
    """在同步代码中跑 async TTS；若已在 FastAPI 事件循环内则切到独立线程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    future = _TTS_EXECUTOR.submit(asyncio.run, coro)
    return future.result(timeout=120)


def _clean_tts_text(text: str, *, max_len: int = 80) -> str:
    line = re.sub(r"\s+", " ", (text or "").strip())
    line = re.sub(r"[《》「」【】]", "", line)
    if len(line) > max_len:
        line = line[: max_len - 1] + "…"
    return line


async def synthesize_to_file(
    text: str,
    dest: Path,
    *,
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    preserve_brackets: bool = False,
) -> bool:
    import edge_tts

    line = (text or "").strip() if preserve_brackets else _clean_tts_text(text)
    line = re.sub(r"\s+", " ", line).strip()
    if len(line) > 80:
        line = line[:79] + "…"
    if not line:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    voice_id = resolve_voice(voice)
    rate_str = rate or os.getenv("HONGGUO_TTS_RATE", DEFAULT_RATE)
    pitch_str = os.getenv("HONGGUO_TTS_PITCH", DEFAULT_PITCH).strip()
    kwargs: dict[str, str] = {"rate": rate_str}
    if pitch_str:
        kwargs["pitch"] = pitch_str
    communicate = edge_tts.Communicate(line, voice_id, **kwargs)
    await communicate.save(str(dest))
    return dest.is_file() and dest.stat().st_size > 200


async def synthesize_lines_batch(
    lines: list[str],
    work_dir: Path,
    *,
    prefix: str = "tts",
    voice: Optional[str] = None,
) -> list[Path]:
    """并行合成多条解说，返回成功生成的 mp3 路径（与 lines 索引对齐）。"""
    cleaned = [_clean_tts_text(x) for x in lines if _clean_tts_text(x)][:3]
    if not cleaned:
        return []

    async def _one(i: int, line: str) -> Optional[Path]:
        path = work_dir / f"{prefix}_{i}.mp3"
        try:
            ok = await synthesize_to_file(line, path, voice=voice)
            return path if ok else None
        except Exception as exc:
            logger.warning("TTS 合成失败 [%s]: %s", line[:20], exc)
            return None

    return await asyncio.gather(*[_one(i, ln) for i, ln in enumerate(cleaned)])


def synthesize_lines_sync(
    lines: list[str],
    work_dir: Path,
    *,
    prefix: str = "tts",
    voice: Optional[str] = None,
) -> list[tuple[str, Path]]:
    """同步封装：返回 [(原文, mp3路径), ...]"""
    if not tts_enabled() or not edge_tts_available():
        return []
    cleaned = [_clean_tts_text(x) for x in lines if _clean_tts_text(x)][:3]
    if not cleaned:
        return []
    try:
        paths = _run_coro_sync(
            synthesize_lines_batch(
                cleaned, work_dir, prefix=prefix, voice=voice
            )
        )
    except Exception as exc:
        logger.warning("TTS 批量合成失败: %s", exc)
        return []

    out: list[tuple[str, Path]] = []
    for line, path in zip(cleaned, paths):
        if path:
            out.append((line, path))
    return out


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def tts_original_volume() -> float:
    return max(0.5, min(1.5, _env_float("HONGGUO_TTS_ORIGINAL_VOLUME", DEFAULT_ORIGINAL_VOLUME)))


def tts_duck_during_narration() -> float:
    return max(0.35, min(1.0, _env_float("HONGGUO_TTS_DUCK_ORIGINAL", DEFAULT_DUCK_DURING_NARR)))


def tts_narration_volume() -> float:
    return max(0.5, min(1.5, _env_float("HONGGUO_TTS_NARR_VOLUME", DEFAULT_NARR_VOLUME)))


def smart_duck_enabled() -> bool:
    v = os.getenv("HONGGUO_TTS_SMART_DUCK", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _merge_narr_windows(
    windows: list[tuple[float, float]],
    total: float,
    *,
    pad: float = 0.25,
) -> list[tuple[float, float]]:
    if not windows:
        return []
    padded = [
        (max(0.0, s - pad), min(total, e + pad))
        for s, e in windows
        if e > s
    ]
    padded.sort(key=lambda x: x[0])
    merged: list[tuple[float, float]] = []
    for s, e in padded:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return merged


def _build_segment_ducked_audio(
    audio,
    windows: list[tuple[float, float]],
    *,
    full_vol: float,
    duck_vol: float,
):
    """解说时段压低原声，其余时段保持正常音量。"""
    from moviepy import concatenate_audioclips

    total = float(audio.duration or 0)
    if total < 0.2 or not windows:
        return audio.with_volume_scaled(full_vol)

    parts = []
    pos = 0.0
    for w_start, w_end in windows:
        if w_start > pos + 0.03:
            parts.append(
                audio.subclipped(pos, w_start).with_volume_scaled(full_vol)
            )
        parts.append(
            audio.subclipped(w_start, w_end).with_volume_scaled(duck_vol)
        )
        pos = w_end
    if pos < total - 0.03:
        parts.append(audio.subclipped(pos, total).with_volume_scaled(full_vol))
    if len(parts) == 1:
        return parts[0]
    return concatenate_audioclips(parts)


def pick_voice_for_episode(seed: str) -> str:
    """多集时可在晓晓/晓伊/云阳间轮换，增加听感变化。"""
    mode = os.getenv("HONGGUO_TTS_VOICE", DEFAULT_VOICE_KEY).strip().lower()
    if mode not in ("rotate", "轮换", "cycle"):
        return resolve_voice()
    keys = ("xiaoxiao", "xiaoyi", "yunyang")
    idx = sum(ord(c) for c in seed) % len(keys)
    return VOICE_PRESETS[keys[idx]]


def mix_narration_on_clip(
    clip,
    lines: list[str],
    work_dir: Path,
    *,
    prefix: str = "narr",
    voice: Optional[str] = None,
    duck_original: Optional[float] = None,
    narr_volume: Optional[float] = None,
    timed_starts: Optional[list[float]] = None,
    narration_only: bool = False,
):
    """叠加解说 TTS；默认仅在解说时段 duck 原声，其余时段保持抓耳原音量。"""
    from moviepy import AudioFileClip, CompositeAudioClip

    if not tts_enabled() or not edge_tts_available():
        return clip

    cleaned = [str(x).strip() for x in lines if str(x).strip()][:3]
    if not cleaned or not clip.duration or clip.duration < 0.5:
        return clip

    full_vol = tts_original_volume()
    duck_vol = duck_original if duck_original is not None else tts_duck_during_narration()
    narr_vol = narr_volume if narr_volume is not None else tts_narration_volume()

    voice_id = voice or resolve_voice()
    pairs = synthesize_lines_sync(
        cleaned, work_dir, prefix=prefix, voice=voice_id
    )
    if not pairs:
        logger.warning("TTS 未生成任何音轨，保留原声")
        return clip

    dur = float(clip.duration)
    narr_layers = []
    narr_windows: list[tuple[float, float]] = []
    for i, (_text, mp3) in enumerate(pairs):
        try:
            ac = AudioFileClip(str(mp3))
            if timed_starts and i < len(timed_starts):
                start = max(0.0, float(timed_starts[i]))
            else:
                slot = dur / len(pairs)
                start = i * slot
            if start + ac.duration > dur:
                ac = ac.subclipped(0, max(0.1, dur - start))
            end = min(dur, start + float(ac.duration))
            if end - start < 0.12:
                continue
            ac = ac.with_start(start)
            if narr_vol != 1.0:
                ac = ac.with_volume_scaled(narr_vol)
            narr_layers.append(ac)
            narr_windows.append((start, end))
        except Exception as exc:
            logger.warning("TTS 音轨加载失败 %s: %s", mp3.name, exc)

    if not narr_layers:
        return clip

    narr_mix = CompositeAudioClip(narr_layers)

    if narration_only or clip.audio is None:
        return clip.with_audio(narr_mix)

    if clip.audio is not None:
        if smart_duck_enabled() and narr_windows and duck_vol < 0.98:
            merged = _merge_narr_windows(narr_windows, dur)
            base = _build_segment_ducked_audio(
                clip.audio,
                merged,
                full_vol=full_vol,
                duck_vol=duck_vol,
            )
            duck_mode = (
                f"分段 duck（常态 {int(full_vol * 100)}% / 解说时 {int(duck_vol * 100)}%）"
            )
        else:
            base = clip.audio.with_volume_scaled(duck_vol)
            duck_mode = f"全程 {duck_vol:.0%}"
        mixed = CompositeAudioClip([base, narr_mix])
    else:
        mixed = narr_mix
        duck_mode = "无原声"

    sync_note = "同步字幕轴" if timed_starts else "均分槽位"
    logger.info(
        "Edge TTS 已混音 %d 条（%s，%s，%s）",
        len(narr_layers),
        voice_id,
        sync_note,
        duck_mode,
    )
    return clip.with_audio(mixed)


async def ensure_search_hint_mp3(
    title: str,
    cache_dir: Path,
    *,
    voice: Optional[str] = None,
) -> Path:
    """生成或读取缓存的首页搜索提示音。"""
    import hashlib

    phrase = search_hint_phrase(title)
    key = hashlib.md5(f"{phrase}:{resolve_voice(voice)}".encode("utf-8")).hexdigest()[:20]
    dest = cache_dir / f"search_hint_{key}.mp3"
    if dest.is_file() and dest.stat().st_size > 200:
        return dest
    ok = await synthesize_to_file(
        phrase, dest, voice=voice, preserve_brackets=True
    )
    if not ok:
        raise RuntimeError("语音合成失败")
    return dest
