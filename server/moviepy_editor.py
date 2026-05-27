"""基于 MoviePy 的成片剪辑（裁剪、倍速、解说叠加、拼接）。默认后端，便于后期扩展 TTS/字幕。"""

from __future__ import annotations

import logging
import os
import random
from pathlib import Path
from typing import Optional

from edge_tts_narration import (
    body_entry_volume,
    card_narration_volume,
    edge_tts_available,
    mix_narration_on_clip,
    pick_voice_for_episode,
    tts_enabled,
    tts_on_body_enabled,
)
from multi_clip import clip_tail_pad_sec
from video_originality import body_burn_subtitles, body_playback_speed
from meme_edit import (
    MemeBeat,
    MemeCaption,
    apply_meme_effects,
    finalize_meme_captions_for_clip,
    meme_on_body_enabled,
)
from video_originality import (
    originality_enabled,
    pick_commentary_lines,
    render_subtitle_overlay_png,
)

logger = logging.getLogger(__name__)

# 默认横屏；生成任务开始时由 output_canvas.activate_canvas 按源片覆盖
WORK_WIDTH = int(os.getenv("HONGGUO_OUTPUT_WIDTH", "1920"))
WORK_HEIGHT = int(os.getenv("HONGGUO_OUTPUT_HEIGHT", "1080"))


def output_size() -> tuple[int, int]:
    return (
        int(os.getenv("HONGGUO_OUTPUT_WIDTH", str(WORK_WIDTH))),
        int(os.getenv("HONGGUO_OUTPUT_HEIGHT", str(WORK_HEIGHT))),
    )
OUTPUT_FPS = int(os.getenv("HONGGUO_OUTPUT_FPS", "30"))
OUTPUT_CRF = int(os.getenv("HONGGUO_OUTPUT_CRF", "20"))
ENCODE_PRESET = os.getenv("HONGGUO_ENCODE_PRESET", "fast")
BODY_ENCODE_PRESET = os.getenv("HONGGUO_BODY_ENCODE_PRESET", "veryfast")


def moviepy_available() -> bool:
    try:
        from moviepy import VideoFileClip  # noqa: F401

        return True
    except ImportError:
        return False


def video_backend() -> str:
    """moviepy | ffmpeg"""
    v = os.getenv("HONGGUO_VIDEO_BACKEND", "moviepy").strip().lower()
    if v == "ffmpeg":
        return "ffmpeg"
    return "moviepy" if moviepy_available() else "ffmpeg"


def _seed_int(seed_str: str) -> int:
    import hashlib

    return int(hashlib.md5(seed_str.encode("utf-8")).hexdigest()[:8], 16)


def _planned_source_span(output_len: float, speed: float) -> float:
    """成片时长 → 从源片截取的秒数（倍速时源片更长）。"""
    if output_len <= 0.01:
        return 0.0
    if abs(speed - 1.0) >= 0.01:
        return output_len * speed
    return output_len


def _open_video_clip_safe(path: Path):
    """打开文件并略裁尾帧，避免 MoviePy 读末帧 0 字节告警。"""
    from moviepy import VideoFileClip

    clip = VideoFileClip(str(path))
    fps = float(clip.fps or OUTPUT_FPS)
    dur = float(clip.duration or 0)
    trim_end = dur - (1.5 / fps)
    if trim_end > 0.05:
        clip = clip.subclipped(0, trim_end)
    return clip


def _write_clip(
    clip,
    dest: Path,
    *,
    preset: str = ENCODE_PRESET,
    audio: bool = True,
    fps: Optional[int] = None,
) -> None:
    from moviepy import VideoFileClip

    dest.parent.mkdir(parents=True, exist_ok=True)
    out_fps = int(fps or getattr(clip, "fps", None) or OUTPUT_FPS)
    kwargs = {
        "fps": out_fps,
        "codec": "libx264",
        "audio": audio and (clip.audio is not None),
        "preset": preset,
        "threads": int(os.getenv("HONGGUO_FFMPEG_THREADS", "4")),
        "logger": None,
        "ffmpeg_params": [
            "-crf",
            str(OUTPUT_CRF),
            "-movflags",
            "+faststart",
            "-vsync",
            "cfr",
        ],
    }
    if kwargs["audio"]:
        kwargs["audio_codec"] = "aac"
        kwargs["audio_bitrate"] = "192k"
    clip.write_videofile(str(dest), **kwargs)
    if hasattr(clip, "close"):
        clip.close()
    elif isinstance(clip, VideoFileClip):
        clip.close()


def letterbox_fit(clip, width: int | None = None, height: int | None = None):
    """等比缩小并居中铺黑边（横屏/竖屏画布由 output_canvas 决定）。"""
    if width is None or height is None:
        ow, oh = output_size()
        width = ow if width is None else width
        height = oh if height is None else height
    from moviepy import ColorClip, CompositeVideoClip

    cw, ch = clip.size
    if cw <= 0 or ch <= 0:
        raise ValueError("无效视频尺寸")
    scale = min(width / cw, height / ch)
    nw = max(2, int(round(cw * scale)))
    nh = max(2, int(round(ch * scale)))
    if nw % 2:
        nw += 1
    if nh % 2:
        nh += 1
    fitted = clip.resized(new_size=(nw, nh))
    bg = ColorClip(size=(width, height), color=(0, 0, 0), duration=clip.duration)
    if clip.audio is not None:
        bg = bg.with_audio(None)
    return CompositeVideoClip(
        [bg, fitted.with_position("center")],
        size=(width, height),
    ).with_duration(clip.duration)


def fit_clip_to_canvas(
    clip,
    width: int | None = None,
    height: int | None = None,
    *,
    seed_str: str = "voiceover",
):
    """口播/正片：与画布同比例时铺满（无黑边），否则 letterbox。"""
    if width is None or height is None:
        ow, oh = output_size()
        width = ow if width is None else width
        height = oh if height is None else height
    cw, ch = float(clip.size[0]), float(clip.size[1])
    if cw <= 1 or ch <= 1:
        return letterbox_fit(clip, width, height)
    try:
        from output_canvas import _use_native_source_size

        native = _use_native_source_size()
    except ImportError:
        native = True
    src_ar = cw / ch
    dst_ar = width / height
    if native or abs(src_ar - dst_ar) / max(dst_ar, 0.01) < 0.08:
        return cover_fit(clip, width, height, seed_str=seed_str)
    return letterbox_fit(clip, width, height)


def cover_fit(
    clip,
    width: int | None = None,
    height: int | None = None,
    *,
    seed_str: str = "",
):
    if width is None or height is None:
        ow, oh = output_size()
        width = ow if width is None else width
        height = oh if height is None else height
    """等比放大居中裁剪填满画布（去重增强用，减少黑边）。"""
    cw, ch = clip.size
    scale = max(width / cw, height / ch)
    nw = max(width, int(round(cw * scale)))
    nh = max(height, int(round(ch * scale)))
    enlarged = clip.resized(new_size=(nw, nh))
    rng = random.Random(_seed_int(seed_str or "cover"))
    x_j = int(rng.uniform(-18, 18))
    y_j = int(rng.uniform(-12, 12))
    x1 = max(0, (nw - width) // 2 + x_j)
    y1 = max(0, (nh - height) // 2 + y_j)
    x2 = min(nw, x1 + width)
    y2 = min(nh, y1 + height)
    return enlarged.cropped(x1=x1, y1=y1, x2=x2, y2=y2)


def add_timed_commentary(
    clip,
    lines: list[str],
    *,
    work_dir: Path,
    prefix: str = "ov",
) -> object:
    """按时间轴叠加底部解说条（PNG → ImageClip）。"""
    from moviepy import CompositeVideoClip, ImageClip

    cleaned = [str(x).strip() for x in lines if str(x).strip()][:5]
    if not cleaned or clip.duration <= 0.5:
        return clip

    ow, oh = output_size()
    layers = [clip]
    slot = clip.duration / len(cleaned)
    for i, line in enumerate(cleaned):
        png = work_dir / f"{prefix}_{i}.png"
        render_subtitle_overlay_png(png, line, width=ow, height=oh)
        ov = (
            ImageClip(str(png))
            .with_duration(min(slot + 0.02, clip.duration - i * slot))
            .with_start(i * slot)
            .with_position((0, 0))
        )
        layers.append(ov)
    return CompositeVideoClip(layers, size=(ow, oh)).with_duration(clip.duration)


def process_body_clip(
    raw: Path,
    dest: Path,
    label: str,
    seconds: float,
    *,
    trim_start_sec: float = 0.0,
    max_duration_sec: Optional[float] = None,
    speed: Optional[float] = None,
    commentary_lines: Optional[list[str]] = None,
    originality_seed: str = "",
    work_dir: Optional[Path] = None,
    need_originality: bool = False,
    meme_captions: Optional[list[MemeCaption]] = None,
    meme_beats: Optional[list[MemeBeat]] = None,
    clip_tail_pad: Optional[float] = None,
    soft_audio_fade_out: bool = False,
    soft_audio_fade_in: bool = False,
) -> None:
    """正片：裁剪 → 画布适配 → 倍速 → meme 特效 → 解说叠加 → 导出。"""
    from moviepy import VideoFileClip

    dest.unlink(missing_ok=True)
    effective_speed = body_playback_speed()
    if speed is not None and abs(float(speed) - effective_speed) > 0.02:
        logger.warning(
            "%s 忽略调用方倍速 %.3g，使用配置 %.3g",
            label,
            float(speed),
            effective_speed,
        )
    speed = effective_speed
    trim_start = max(0.0, float(trim_start_sec or 0))

    output_len = (
        float(max_duration_sec) if max_duration_sec and max_duration_sec > 0 else 0.0
    )
    use_speed = abs(speed - 1.0) >= 0.01
    source_len = _planned_source_span(output_len, speed)
    tail_pad = clip_tail_pad if clip_tail_pad is not None else 0.0
    if tail_pad <= 0 and output_len > 0.01:
        tail_pad = clip_tail_pad_sec()

    clip = VideoFileClip(str(raw))
    try:
        raw_dur = float(clip.duration or 0)
        if source_len > 0.01:
            end = trim_start + source_len + max(0.0, tail_pad)
            if raw_dur > 0.1:
                end = min(end, max(trim_start + 0.3, raw_dur - 0.05))
        else:
            end = None
        sub = clip.subclipped(trim_start, end)

        ow, oh = output_size()
        if need_originality:
            sub = cover_fit(sub, ow, oh, seed_str=originality_seed or label)
        else:
            sub = fit_clip_to_canvas(
                sub, ow, oh, seed_str=originality_seed or label
            )

        if use_speed:
            sub = sub.with_speed_scaled(speed)

        if sub.duration and sub.duration > 0.45:
            try:
                from moviepy import afx

                effects = []
                if soft_audio_fade_in:
                    fade_in = min(
                        0.65,
                        max(0.12, float(sub.duration) * 0.12),
                    )
                    effects.append(afx.AudioFadeIn(fade_in))
                    entry_vol = body_entry_volume()
                    if entry_vol < 0.999 and sub.audio is not None:
                        sub = sub.with_audio(
                            sub.audio.with_volume_scaled(entry_vol)
                        )
                if soft_audio_fade_out:
                    fade_out = min(0.35, max(0.12, float(sub.duration) * 0.12))
                    effects.append(afx.AudioFadeOut(fade_out))
                if effects:
                    sub = sub.with_effects(effects)
            except Exception as exc:
                logger.debug("%s 段音淡入淡出跳过: %s", label, exc)

        final_meme_caps: list[MemeCaption] = []
        if meme_on_body_enabled() and work_dir and (meme_captions or meme_beats):
            final_meme_caps = finalize_meme_captions_for_clip(
                meme_captions or [],
                float(sub.duration or 0),
                max_count=3,
            )
            sub = apply_meme_effects(
                sub,
                captions=final_meme_caps,
                beats=(meme_beats or [])[:2],
                work_dir=work_dir,
                prefix=f"{dest.stem}_meme",
                seed_str=originality_seed or label,
            )

        use_meme_visual = bool(final_meme_caps)
        if (
            need_originality
            and body_burn_subtitles()
            and commentary_lines
            and work_dir
            and sub.duration
            and sub.duration > 0.5
            and not use_meme_visual
        ):
            sub = add_timed_commentary(
                sub,
                commentary_lines[:3],
                work_dir=work_dir,
                prefix=f"{dest.stem}_ov",
            )

        if (
            tts_on_body_enabled()
            and edge_tts_available()
            and work_dir
            and sub.duration
            and sub.duration > 0.5
        ):
            tts_lines = (
                [c.text for c in final_meme_caps]
                if final_meme_caps
                else (commentary_lines or [])[:3]
            )
            if tts_lines:
                voice = pick_voice_for_episode(originality_seed or label)
                timed_starts = [c.at_sec for c in final_meme_caps] if final_meme_caps else None
                sub = mix_narration_on_clip(
                    sub,
                    tts_lines,
                    work_dir,
                    prefix=f"{dest.stem}_tts",
                    voice=voice,
                    timed_starts=timed_starts,
                )

        preset = BODY_ENCODE_PRESET if need_originality else ENCODE_PRESET
        logger.info(
            "%s 正在编码导出（MoviePy，倍速 %.3gx，约需数分钟）…",
            label,
            speed,
        )
        _write_clip(
            sub,
            dest,
            preset=preset,
            audio=True,
            fps=int(getattr(sub, "fps", None) or OUTPUT_FPS),
        )
    finally:
        clip.close()

    if not dest.is_file() or dest.stat().st_size < 10_000:
        raise RuntimeError(f"{label} MoviePy 正片处理失败")


def image_to_video(
    image: Path,
    dest: Path,
    duration: float,
    *,
    fps: int = OUTPUT_FPS,
    narration_text: str = "",
    work_dir: Optional[Path] = None,
    narration_voice: Optional[str] = None,
) -> None:
    """静态图 → 短视频（片头/解说卡/片尾）；可选 Edge TTS 解说。"""
    import numpy as np
    from moviepy import AudioClip, ImageClip

    dest.unlink(missing_ok=True)
    ow, oh = output_size()
    clip = ImageClip(str(image)).with_duration(duration)
    clip = letterbox_fit(clip, ow, oh)

    narr = (narration_text or "").strip()
    if narr and work_dir and tts_enabled() and edge_tts_available():
        clip = mix_narration_on_clip(
            clip,
            [narr],
            work_dir,
            prefix=f"{dest.stem}_card",
            voice=narration_voice,
            narration_only=True,
            narr_volume=card_narration_volume(),
        )
    else:

        def make_silence(t):
            return np.zeros(2, dtype=np.float64)

        audio = AudioClip(make_silence, duration=duration, fps=44100)
        clip = clip.with_audio(audio)

    _write_clip(clip, dest, preset=ENCODE_PRESET, audio=clip.audio is not None)


def normalize_video_file(
    src: Path,
    dest: Path,
    *,
    seconds: float = 0.0,
) -> None:
    """统一为当前成片画布（与 output_canvas / 环境变量一致）。"""
    from moviepy import VideoFileClip

    ow, oh = output_size()
    dest.unlink(missing_ok=True)
    clip = _open_video_clip_safe(src)
    try:
        fitted = letterbox_fit(clip, ow, oh)
        _write_clip(fitted, dest, preset=ENCODE_PRESET, audio=clip.audio is not None)
    finally:
        clip.close()


def merge_video_files(
    parts: list[Path],
    dest: Path,
    *,
    crossfade_sec: Optional[float] = None,
) -> None:
    """多段拼接；crossfade_sec>0 时段间交叉淡化。"""
    from moviepy import concatenate_videoclips
    from moviepy import vfx

    from multi_clip import clip_crossfade_sec

    dest.unlink(missing_ok=True)
    cf = clip_crossfade_sec() if crossfade_sec is None else max(0.0, crossfade_sec)
    clips = []
    try:
        for p in parts:
            clips.append(_open_video_clip_safe(p))
        if not clips:
            raise RuntimeError("无片段可拼接")
        ow, oh = output_size()
        fitted_clips = []
        for c in clips:
            cw, ch = c.size
            if int(cw) != ow or int(ch) != oh:
                c = fit_clip_to_canvas(c, ow, oh, seed_str=str(p))
            fitted_clips.append(c)
        clips = fitted_clips
        if cf > 0.02 and len(clips) > 1:
            prepared = []
            for i, c in enumerate(clips):
                if i > 0:
                    c = c.with_effects([vfx.CrossFadeIn(cf)])
                if i < len(clips) - 1:
                    c = c.with_effects([vfx.CrossFadeOut(cf)])
                prepared.append(c)
            final = concatenate_videoclips(
                prepared, method="compose", padding=-cf
            )
        else:
            final = concatenate_videoclips(clips, method="compose")
        _write_clip(final, dest, preset=ENCODE_PRESET, audio=True)
    finally:
        for c in clips:
            try:
                c.close()
            except Exception:
                pass


def build_commentary_lines_for_plan(
    *,
    plan_lines: list[str],
    opening_text: str,
    subtitle_hint: str,
    hook_summary: str,
    drama_title: str,
) -> list[str]:
    return pick_commentary_lines(
        plan_lines=plan_lines,
        opening_text=opening_text,
        subtitle_hint=subtitle_hint,
        hook_summary=hook_summary,
        drama_title=drama_title,
    )
