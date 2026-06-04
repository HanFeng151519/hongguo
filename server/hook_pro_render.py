"""专业 60s 钩子：黄金口播 / 定格 / 尾帧 CTA 渲染。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from hook_timeline import (
    fixed_opening_line,
    fixed_outro_line,
    freeze_caption_text,
    freeze_hold_sec,
    golden_open_sec,
    opening_bgm_path,
    outro_cta_sec,
)

logger = logging.getLogger(__name__)


def _canvas_wh() -> tuple[int, int]:
    from output_canvas import output_size

    return output_size()


def _safe_video_subclip(clip, end_sec: float):
    """裁切终点不得超过实测视频时长（避免 MoviePy 音画时长不一致报错）。"""
    dur = float(clip.duration or 0.0)
    if dur <= 0.05:
        return clip
    end = min(float(end_sec), dur - 0.02)
    if end <= 0.1 or end >= dur - 0.01:
        return clip
    return clip.subclipped(0, end)


def voiceover_subtitle_enabled() -> bool:
    """口播字幕开关：默认关闭，仅保留配音与原声混音。"""
    v = os.getenv("HONGGUO_VOICEOVER_SUBTITLE", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def outro_flash_enabled() -> bool:
    """片尾高光闪帧开关：默认开。"""
    v = os.getenv("HONGGUO_OUTRO_FLASH", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _build_outro_flash_clip(clip, *, flash_frames: int = 12, flash_sec: float = 3.0):
    """将片尾改为高光闪帧（默认 12 帧 / 3 秒）。"""
    from moviepy import ColorClip, ImageClip, concatenate_videoclips

    total = float(clip.duration or 0.0)
    if total <= 0.1:
        return clip
    n = max(4, int(flash_frames))
    dur = max(1.0, float(flash_sec))
    frame_slot = dur / n
    # 每个槽位做“画面+黑帧”闪切，视觉上更明显（不再像正常播放）
    pic_dur = max(0.05, min(frame_slot * 0.72, frame_slot - 0.04))
    gap_dur = max(0.01, frame_slot - pic_dur)
    if total < pic_dur:
        return clip
    # 在尾段均匀抽帧，做“闪过”效果
    start = max(0.0, total - dur)
    times = [start + (dur * i / max(1, n - 1)) for i in range(n)]
    imgs = []
    for t in times:
        imgs.append(ImageClip(clip.get_frame(t)).with_duration(pic_dur))
        imgs.append(
            ColorClip(size=clip.size, color=(0, 0, 0)).with_duration(gap_dur)
        )
    out = concatenate_videoclips(imgs, method="chain").with_duration(dur)
    return out


def render_center_subtitle_png(
    path: Path,
    text: str,
    *,
    width: int | None = None,
    height: int | None = None,
    font_px: int | None = None,
) -> None:
    from output_canvas import output_size, scaled_px

    if width is None or height is None:
        ow, oh = output_size()
        width = ow if width is None else width
        height = oh if height is None else height
    if font_px is None:
        font_px = scaled_px(72)
    else:
        font_px = scaled_px(font_px)
    """大字加粗居中字幕条（透明底 PNG）。"""
    from PIL import Image, ImageDraw, ImageFont

    line = (text or "").strip()[:40]
    if not line:
        line = " "

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", font_px)
    except OSError:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/STHeiti Medium.ttc", font_px)
        except OSError:
            font = ImageFont.load_default()

    box = draw.textbbox((0, 0), line, font=font, stroke_width=4)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = (width - tw) // 2 - box[0]
    y = (height - th) // 2 - box[1]
    draw.text(
        (x, y),
        line,
        fill=(255, 255, 255, 255),
        font=font,
        stroke_width=4,
        stroke_fill=(0, 0, 0, 220),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _mix_bgm_under_voice(
    clip,
    bgm_path: Path,
    *,
    bgm_vol: float = 0.22,
    voice_vol: float = 1.0,
):
    """轻 BGM 垫底（音量低于口播）。"""
    from moviepy import AudioFileClip, CompositeAudioClip

    if not bgm_path.is_file():
        return clip
    try:
        bgm = AudioFileClip(str(bgm_path)).with_volume_scaled(bgm_vol)
        dur = float(clip.duration or 0)
        if bgm.duration and bgm.duration < dur:
            from moviepy import concatenate_audioclips

            reps = int(dur / bgm.duration) + 1
            bgm = concatenate_audioclips([bgm] * reps).subclipped(0, dur)
        else:
            bgm = bgm.subclipped(0, dur)
        if clip.audio is not None:
            voice = clip.audio.with_volume_scaled(voice_vol)
            mixed = CompositeAudioClip([bgm, voice.with_start(0)])
        else:
            mixed = bgm
        return clip.with_audio(mixed)
    except Exception as exc:
        logger.warning("片头 BGM 混音失败: %s", exc)
        return clip


def enhance_golden_opening_clip(
    clip_path: Path,
    *,
    opening_text: str,
    work_dir: Path,
    voice: Optional[str] = None,
    dest: Optional[Path] = None,
) -> Path:
    """正片高能片段 + 居中字幕 + TTS + 可选 BGM。"""
    from edge_tts_narration import (
        edge_tts_available,
        mix_narration_on_clip,
        opening_card_duration_for_text,
        probe_media_duration,
        resolve_voice,
        synthesize_lines_sync,
        tts_enabled,
    )
    from moviepy import CompositeVideoClip, ImageClip, VideoFileClip
    from moviepy_editor import _write_clip, fit_clip_to_canvas

    out = dest or clip_path
    line = (opening_text or fixed_opening_line()).strip()
    max_dur = golden_open_sec()
    tail_pad = max(
        0.0,
        min(0.2, float(os.getenv("HONGGUO_GOLDEN_TTS_TAIL_PAD", "0.06") or 0.06)),
    )

    clip = VideoFileClip(str(clip_path))
    try:
        ow, oh = _canvas_wh()
        fitted = fit_clip_to_canvas(clip, ow, oh, seed_str="golden_open")
        if fitted.duration and fitted.duration > max_dur + 0.05:
            fitted = _safe_video_subclip(fitted, max_dur)

        if tts_enabled() and edge_tts_available() and line:
            voice_id = voice or resolve_voice()
            pairs = synthesize_lines_sync(
                [line], work_dir, prefix="golden_tts", voice=voice_id
            )
            narr_dur = 0.0
            if pairs:
                narr_dur = probe_media_duration(pairs[0][1])
            if narr_dur < 0.2:
                _, narr_dur = opening_card_duration_for_text(
                    line, work_dir, voice=voice_id
                )
            target_len = min(max_dur, max(0.35, narr_dur + tail_pad))
            vd = float(fitted.duration or target_len)
            if vd > target_len + 0.02:
                fitted = _safe_video_subclip(fitted, target_len)
            elif vd + 0.05 < narr_dur and narr_dur > 0.2:
                fitted = fitted.with_duration(narr_dur + tail_pad)

            from edge_tts_narration import golden_opening_duck_volume

            duck = golden_opening_duck_volume()
            from edge_tts_narration import card_narration_volume

            fitted = mix_narration_on_clip(
                fitted,
                [line],
                work_dir,
                prefix="golden_tts",
                voice=voice_id,
                narration_only=False,
                duck_original=duck,
                narr_volume=card_narration_volume(),
            )
            logger.info("黄金口播：原声 duck 至约 %d%%", int(duck * 100))
        elif fitted.audio is not None:
            from edge_tts_narration import golden_opening_duck_volume

            fitted = fitted.with_audio(
                fitted.audio.with_volume_scaled(golden_opening_duck_volume())
            )

        bgm = opening_bgm_path()
        if bgm:
            fitted = _mix_bgm_under_voice(fitted, Path(bgm))

        if voiceover_subtitle_enabled():
            sub_png = work_dir / "golden_center_sub.png"
            render_center_subtitle_png(sub_png, line, width=ow, height=oh)
            ov = (
                ImageClip(str(sub_png))
                .with_duration(float(fitted.duration))
                .with_position((0, 0))
            )
            final = CompositeVideoClip([fitted, ov], size=(ow, oh)).with_duration(
                float(fitted.duration)
            )
        else:
            final = fitted
        _write_clip(final, out, preset="fast", audio=final.audio is not None)
        if final is not fitted:
            final.close()
        fitted.close()
    finally:
        clip.close()
    return out


def enhance_outro_voiceover_clip(
    clip_path: Path,
    *,
    outro_text: str,
    work_dir: Path,
    voice: Optional[str] = None,
    dest: Optional[Path] = None,
) -> Path:
    """正片末段画面 + 居中字幕 + 片尾口播 TTS（与主视频同分辨率）。"""
    from edge_tts_narration import (
        card_narration_volume,
        edge_tts_available,
        golden_opening_duck_volume,
        mix_narration_on_clip,
        opening_card_duration_for_text,
        probe_media_duration,
        resolve_voice,
        synthesize_lines_sync,
        tts_enabled,
    )
    from moviepy import CompositeVideoClip, ImageClip, VideoFileClip
    from moviepy_editor import _write_clip, fit_clip_to_canvas

    out = dest or clip_path
    line = (outro_text or fixed_outro_line()).strip()
    max_dur = outro_cta_sec()
    tail_pad = max(
        0.0,
        min(0.2, float(os.getenv("HONGGUO_OUTRO_TTS_TAIL_PAD", "0.06") or 0.06)),
    )

    clip = VideoFileClip(str(clip_path))
    try:
        ow, oh = _canvas_wh()
        fitted = fit_clip_to_canvas(clip, ow, oh, seed_str="outro_voice")
        if fitted.duration and fitted.duration > max_dur + 0.05:
            fitted = _safe_video_subclip(fitted, max_dur)

        if outro_flash_enabled():
            frames = max(
                4, int(float(os.getenv("HONGGUO_OUTRO_FLASH_FRAMES", "12") or 12))
            )
            sec = max(1.0, float(os.getenv("HONGGUO_OUTRO_FLASH_SEC", "3") or 3.0))
            fitted = _build_outro_flash_clip(
                fitted,
                flash_frames=frames,
                flash_sec=sec,
            )
            logger.info("片尾高光闪帧：%d 帧 / %.1fs", frames, sec)

        if tts_enabled() and edge_tts_available() and line:
            voice_id = voice or resolve_voice()
            pairs = synthesize_lines_sync(
                [line], work_dir, prefix="outro_tts", voice=voice_id
            )
            narr_dur = 0.0
            if pairs:
                narr_dur = probe_media_duration(pairs[0][1])
            if narr_dur < 0.2:
                _, narr_dur = opening_card_duration_for_text(
                    line, work_dir, voice=voice_id
                )
            target_len = min(max_dur, max(0.35, narr_dur + tail_pad))
            vd = float(fitted.duration or target_len)
            if vd > target_len + 0.02:
                fitted = _safe_video_subclip(fitted, target_len)
            elif vd + 0.05 < narr_dur and narr_dur > 0.2:
                fitted = fitted.with_duration(narr_dur + tail_pad)

            duck = golden_opening_duck_volume()
            fitted = mix_narration_on_clip(
                fitted,
                [line],
                work_dir,
                prefix="outro_tts",
                voice=voice_id,
                narration_only=False,
                duck_original=duck,
                narr_volume=card_narration_volume(),
            )
            logger.info("片尾口播：原声 duck 至约 %d%%", int(duck * 100))
        elif fitted.audio is not None:
            fitted = fitted.with_audio(
                fitted.audio.with_volume_scaled(golden_opening_duck_volume())
            )

        if voiceover_subtitle_enabled():
            sub_png = work_dir / "outro_center_sub.png"
            render_center_subtitle_png(sub_png, line, width=ow, height=oh)
            ov = (
                ImageClip(str(sub_png))
                .with_duration(float(fitted.duration))
                .with_position((0, 0))
            )
            final = CompositeVideoClip([fitted, ov], size=(ow, oh)).with_duration(
                float(fitted.duration)
            )
        else:
            final = fitted
        _write_clip(final, out, preset="fast", audio=final.audio is not None)
        if final is not fitted:
            final.close()
        fitted.close()
    finally:
        clip.close()
    return out


def apply_tail_fade_inplace(
    video: Path,
    work_dir: Path,
    *,
    fade_sec: float = 1.0,
) -> bool:
    """在最后一镜末尾做画面+原声淡出到黑（不插黑场卡、不额外加长）。"""
    from hook_generator import _probe_duration, _run_ffmpeg

    work_dir.mkdir(parents=True, exist_ok=True)
    total = _probe_duration(video) or 0.0
    if total < 0.45 or fade_sec <= 0.05:
        return False
    fade_d = min(float(fade_sec), max(0.4, total * 0.22), total - 0.06)
    fade_st = max(0.0, total - fade_d)
    tmp = work_dir / f"{video.stem}_tailfade.mp4"
    tmp.unlink(missing_ok=True)

    ow, oh = _canvas_wh()
    vf = (
        f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
        f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fade=t=out:st={fade_st:.3f}:d={fade_d:.3f}:color=black,format=yuv420p"
    )
    cmd = [
        "-hide_banner",
        "-i",
        str(video),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]
    has_audio = True
    try:
        import subprocess

        from ffmpeg_util import resolve_ffprobe_exe

        ffprobe = resolve_ffprobe_exe()
        if ffprobe:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "csv=p=0",
                    str(video),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            has_audio = bool((proc.stdout or "").strip())
    except (OSError, subprocess.TimeoutExpired):
        pass

    if has_audio:
        cmd.extend(
            [
                "-af",
                f"afade=t=out:st={fade_st:.3f}:d={fade_d:.3f}",
                "-c:a",
                "aac",
            ]
        )
    else:
        cmd.append("-an")

    cmd.append(str(tmp))
    try:
        _run_ffmpeg(cmd, timeout=180)
    except Exception as exc:
        logger.warning("最后一镜淡出失败 %s: %s", video.name, exc)
        tmp.unlink(missing_ok=True)
        return False
    if not tmp.is_file() or tmp.stat().st_size < 8000:
        tmp.unlink(missing_ok=True)
        return False
    video.unlink(missing_ok=True)
    tmp.rename(video)
    logger.info("正片最后一镜末尾淡出 %.2fs（%s）", fade_d, video.name)
    return True


def build_black_caption_fadeout(
    dest: Path,
    *,
    work_dir: Path,
    duration: float,
    caption: str,
) -> None:
    """黑场 + 悬念字幕，不复播正片最后一镜（避免结局重复）。"""
    from hook_generator import _run_ffmpeg

    fade = max(0.8, min(2.5, float(duration)))
    cap = (caption or freeze_caption_text()).strip()
    dest.unlink(missing_ok=True)
    from output_canvas import scaled_px

    ow, oh = _canvas_wh()

    if cap:
        sub = work_dir / "fadeout_sub_black.png"
        render_center_subtitle_png(sub, cap, font_px=scaled_px(56), width=ow, height=oh)
        _run_ffmpeg(
            [
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={ow}x{oh}:r=30:d={fade:.3f}",
                "-loop",
                "1",
                "-i",
                str(sub),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                f"{fade:.3f}",
                "-filter_complex",
                (
                    "[0:v][1:v]overlay=0:0:format=auto,"
                    f"fade=t=in:st=0:d={min(0.35, fade * 0.4):.3f}:color=black,"
                    "format=yuv420p[v]"
                ),
                "-map",
                "[v]",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            timeout=120,
        )
    else:
        _run_ffmpeg(
            [
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={ow}x{oh}:r=30:d={fade:.3f}",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                f"{fade:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            timeout=120,
        )
    logger.info("正片尾黑场字幕 %.2fs（不重复末段画面）", fade)


def build_body_fadeout_segment(
    source_video: Path,
    dest: Path,
    *,
    work_dir: Path,
    at_ratio: float = 0.92,
    duration: Optional[float] = None,
    caption: Optional[str] = None,
) -> None:
    """正片尾：取末段画面/原声淡出到黑 + 悬念字幕（比静帧定格更自然）。"""
    from hook_generator import _probe_duration, _run_ffmpeg

    from hook_timeline import outro_tail_video_sec

    fade = duration if duration is not None else freeze_hold_sec()
    cap = (caption or freeze_caption_text()).strip()
    max_vid = outro_tail_video_sec()
    if max_vid <= 0.05:
        build_black_caption_fadeout(
            dest, work_dir=work_dir, duration=fade, caption=cap
        )
        return

    fade = max(0.7, min(2.5, float(fade)))
    total = _probe_duration(source_video) or 10.0
    tail = min(max(fade + max_vid, fade + 0.15), max_vid + fade, total - 0.05)
    ss = max(0.0, total - tail)
    fade_d = min(fade, max(0.5, tail - 0.08))
    fade_st = max(0.0, tail - fade_d)
    dest.unlink(missing_ok=True)
    from output_canvas import scaled_px

    ow, oh = _canvas_wh()
    scale_fade = (
        f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
        f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fade=t=out:st={fade_st:.3f}:d={fade_d:.3f}:color=black"
    )

    if cap:
        sub = work_dir / "fadeout_sub.png"
        render_center_subtitle_png(sub, cap, font_px=scaled_px(56), width=ow, height=oh)
        _run_ffmpeg(
            [
                "-hide_banner",
                "-ss",
                f"{ss:.3f}",
                "-i",
                str(source_video),
                "-loop",
                "1",
                "-i",
                str(sub),
                "-t",
                f"{tail:.3f}",
                "-filter_complex",
                (
                    f"[0:v]{scale_fade}[v0];"
                    f"[1:v]scale={ow}:{oh}[sub];[v0][sub]overlay=0:0:format=auto,format=yuv420p[v];"
                    f"[0:a]afade=t=out:st={fade_st:.3f}:d={fade_d:.3f}[a]"
                ),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            timeout=120,
        )
    else:
        _run_ffmpeg(
            [
                "-hide_banner",
                "-ss",
                f"{ss:.3f}",
                "-i",
                str(source_video),
                "-t",
                f"{tail:.3f}",
                "-vf",
                f"{scale_fade},format=yuv420p",
                "-af",
                f"afade=t=out:st={fade_st:.3f}:d={fade_d:.3f}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                str(dest),
            ],
            timeout=120,
        )
    logger.info("正片尾淡出 %.2fs（画面+原声）", tail)
    return


def build_freeze_segment(
    source_video: Path,
    dest: Path,
    *,
    work_dir: Path,
    at_ratio: float = 0.92,
    duration: Optional[float] = None,
    caption: Optional[str] = None,
) -> None:
    """从正片截取一帧定格 + 悬念字幕（HONGGUO_BODY_OUTRO_FADE=0 时使用）。"""
    from hook_timeline import body_outro_use_fade

    if body_outro_use_fade():
        build_body_fadeout_segment(
            source_video,
            dest,
            work_dir=work_dir,
            at_ratio=at_ratio,
            duration=duration,
            caption=caption,
        )
        return

    from hook_generator import _probe_duration, _run_ffmpeg

    dur = duration if duration is not None else freeze_hold_sec()
    cap = (caption or freeze_caption_text()).strip()
    total = _probe_duration(source_video) or 10.0
    ss = max(0.0, min(total - 0.1, total * at_ratio))
    frame = work_dir / "freeze_frame.png"
    dest.unlink(missing_ok=True)

    _run_ffmpeg(
        [
            "-hide_banner",
            "-ss",
            f"{ss:.3f}",
            "-i",
            str(source_video),
            "-vframes",
            "1",
            "-q:v",
            "2",
            str(frame),
        ],
        timeout=60,
    )

    from output_canvas import scaled_px

    ow, oh = _canvas_wh()
    if cap:
        sub = work_dir / "freeze_sub.png"
        render_center_subtitle_png(sub, cap, font_px=scaled_px(56), width=ow, height=oh)
        _run_ffmpeg(
            [
                "-hide_banner",
                "-loop",
                "1",
                "-i",
                str(frame),
                "-loop",
                "1",
                "-i",
                str(sub),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                str(dur),
                "-filter_complex",
                f"[0:v]scale={ow}:{oh}[v0];[1:v]scale={ow}:{oh}[sub];"
                "[v0][sub]overlay=0:0:format=auto,format=yuv420p[v]",
                "-map",
                "[v]",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            timeout=120,
        )
    else:
        _run_ffmpeg(
            [
                "-hide_banner",
                "-loop",
                "1",
                "-i",
                str(frame),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t",
                str(dur),
                "-vf",
                f"scale={ow}:{oh}:force_original_aspect_ratio=decrease,"
                f"pad={ow}:{oh}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-shortest",
                str(dest),
            ],
            timeout=120,
        )


