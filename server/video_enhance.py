"""Real-ESRGAN 视频超分（ncnn-vulkan：抽帧 → 超分 → 合成）。"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import zipfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

ProgressFn = Callable[[str, float, int | None], None]


def _eta_from_frac(elapsed: float, frac: float) -> int | None:
    if frac <= 0.03 or frac >= 0.99 or elapsed < 1:
        return None
    return max(1, int(elapsed / frac * (1.0 - frac)))


def _eta_from_frames(elapsed: float, done: int, total: int) -> int | None:
    if done < 1 or total <= done:
        return None
    return max(1, int(elapsed / done * (total - done)))

from ffmpeg_util import resolve_ffmpeg_exe

logger = logging.getLogger(__name__)

# v2：抽帧目录超分（勿再向 ncnn 传 .mp4 路径）
SR_PIPELINE_VERSION = 2

TOOLS_DIR = Path(__file__).resolve().parent / "tools" / "realesrgan-ncnn-vulkan"

_NCNN_RELEASES = {
    "windows": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
        "realesrgan-ncnn-vulkan-20220424-windows.zip"
    ),
    "linux": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
        "realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
    ),
    "darwin": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
        "realesrgan-ncnn-vulkan-20220424-macos.zip"
    ),
}

_MODEL_ALIASES = {
    "realesr-general-x4v3": "realesrgan-x4plus",
    "general": "realesrgan-x4plus",
    "realesrgan-x4plus": "realesrgan-x4plus",
    "anime": "realesr-animevideov3",
    "realesr-animevideov3": "realesr-animevideov3",
    "realesrgan-x4plus-anime": "realesrgan-x4plus-anime",
}


def normalize_enhance(value: str | None) -> str:
    v = (value or os.getenv("HONGGUO_WM_ENHANCE", "")).strip().lower()
    if v in ("0", "off", "none", "false", "no", "关闭", ""):
        return "off"
    if v in ("sr", "ai", "realesrgan", "超分", "1", "true", "on"):
        return "sr"
    return "off"


def sr_enabled(value: str | None = None) -> bool:
    return normalize_enhance(value) == "sr"


def _platform_key() -> str:
    sys = platform.system().lower()
    if sys == "windows":
        return "windows"
    if sys == "darwin":
        return "darwin"
    return "linux"


def _ncnn_exe_name() -> str:
    return "realesrgan-ncnn-vulkan.exe" if _platform_key() == "windows" else "realesrgan-ncnn-vulkan"


def _ensure_executable(path: Path) -> None:
    """zip 解压后 macOS/Linux 可执行位可能丢失。"""
    if platform.system() == "Windows" or not path.is_file():
        return
    mode = path.stat().st_mode
    if mode & stat.S_IXUSR:
        return
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _chmod_ncnn_tree(root: Path) -> None:
    if platform.system() == "Windows":
        return
    for candidate in root.rglob(_ncnn_exe_name()):
        if candidate.is_file():
            _ensure_executable(candidate)


def find_ncnn_exe() -> Path | None:
    bundled = TOOLS_DIR / _ncnn_exe_name()
    if bundled.is_file():
        _ensure_executable(bundled)
        return bundled
    env = os.getenv("HONGGUO_REALESRGAN_BIN", "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
    found = shutil.which(_ncnn_exe_name())
    if found:
        return Path(found)
    return None


def ensure_ncnn_bundle() -> Path:
    exe = find_ncnn_exe()
    if exe is not None:
        return exe

    key = _platform_key()
    url = _NCNN_RELEASES.get(key)
    if not url:
        raise RuntimeError(f"当前系统 {platform.system()} 暂无内置 Real-ESRGAN 包，请手动安装 ncnn-vulkan")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TOOLS_DIR / "realesrgan-ncnn-vulkan.zip"
    logger.info("正在下载 Real-ESRGAN ncnn-vulkan（约 40MB，仅首次）…")
    with httpx.Client(timeout=600.0, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_bytes(1024 * 256):
                    fh.write(chunk)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(TOOLS_DIR)
    zip_path.unlink(missing_ok=True)
    _chmod_ncnn_tree(TOOLS_DIR)

    exe = find_ncnn_exe()
    if exe is None:
        for candidate in TOOLS_DIR.rglob(_ncnn_exe_name()):
            if candidate.is_file():
                _ensure_executable(candidate)
                return candidate
        raise RuntimeError("Real-ESRGAN 解压后未找到可执行文件")
    return exe


def sr_available() -> bool:
    try:
        return find_ncnn_exe() is not None or _platform_key() in _NCNN_RELEASES
    except OSError:
        return False


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _sr_max_input_long_edge() -> int:
    """与 iOS 本机超分一致：先缩小再 AI，显著提速。"""
    return max(128, _env_int("HONGGUO_SR_MAX_INPUT_LONG_EDGE", 512))


def _prep_scale_dims(w: int, h: int, max_long: int) -> tuple[int, int] | None:
    w, h = max(2, w), max(2, h)
    if max(w, h) <= max_long:
        return None
    if w >= h:
        new_h = max_long
        new_w = max(2, int(round(w * max_long / h)))
    else:
        new_w = max_long
        new_h = max(2, int(round(h * max_long / w)))
    new_w -= new_w % 2
    new_h -= new_h % 2
    return new_w, new_h


def _vf_cap_long_edge(w: int, h: int, max_long: int) -> str | None:
    dims = _prep_scale_dims(w, h, max_long)
    if not dims:
        return None
    return f"scale={dims[0]}:{dims[1]}:flags=lanczos"


def _probe_video_fps(path: Path) -> float:
    ffprobe = None
    try:
        from ffmpeg_util import resolve_ffprobe_exe

        ffprobe = resolve_ffprobe_exe()
    except Exception:
        pass
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=avg_frame_rate,r_frame_rate",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    if "/" in line:
                        num, den = line.strip().split("/", 1)
                        try:
                            d = float(den)
                            if d > 0:
                                fps = float(num) / d
                                if fps >= 1:
                                    return fps
                        except ValueError:
                            continue
        except OSError:
            pass

    ffmpeg = resolve_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        m = re.search(r"(\d+(?:\.\d+)?)\s*fps", line, re.I)
        if m:
            return max(1.0, float(m.group(1)))
    return 30.0


def _resolve_model_name(model: str, models_dir: Path) -> str:
    raw = (model or os.getenv("HONGGUO_REALESRGAN_MODEL", "realesr-animevideov3")).strip()
    name = _MODEL_ALIASES.get(raw, raw)
    candidates = [name, "realesr-animevideov3", "realesrgan-x4plus", "realesrgan-x4plus-anime"]
    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if (models_dir / f"{cand}.param").is_file():
            return cand
        if list(models_dir.glob(f"{cand}*.param")):
            return cand
    return "realesrgan-x4plus"


def calc_sr_scale(
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
) -> int:
    """Real-ESRGAN 支持 1–4 倍；默认至少 2 倍以获得可见画质提升。"""
    if src_w < 2 or src_h < 2:
        return 2
    ratio = min(target_w / src_w, target_h / src_h)
    if ratio >= 1.05:
        return max(2, min(4, int(round(ratio))))
    return max(2, min(4, _env_int("HONGGUO_SR_SAME_RES_SCALE", 2)))


def _ncnn_scale(model_name: str, desired: int) -> int:
    """x4plus 仅有 4x 权重；动漫 v3 支持 2/3/4。"""
    desired = max(2, min(4, int(desired)))
    if model_name in ("realesrgan-x4plus", "realesrgan-x4plus-anime"):
        return 4
    return desired


def _extract_frames(
    src: Path,
    frames_dir: Path,
    *,
    fmt: str,
    vf: str | None = None,
) -> float:
    frames_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg_exe()
    pattern = frames_dir / f"frame%08d.{fmt}"
    jpeg_q = max(2, min(5, _env_int("HONGGUO_REALESRGAN_JPEG_QUALITY", 2)))
    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
    ]
    if vf:
        args.extend(["-vf", vf])
    args.extend(
        [
            "-qscale:v",
            str(jpeg_q),
            "-vsync",
            "0",
            str(pattern),
        ]
    )
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise RuntimeError(f"抽帧失败: {tail}")
    frames = list(frames_dir.glob(f"frame*.{fmt}"))
    if not frames:
        raise RuntimeError("抽帧后未得到任何画面")
    return _probe_video_fps(src)


def _merge_frames(
    frames_dir: Path,
    dest: Path,
    *,
    fmt: str,
    fps: float,
) -> None:
    ffmpeg = resolve_ffmpeg_exe()
    pattern = frames_dir / f"frame%08d.{fmt}"
    frames = list(frames_dir.glob(f"frame*.{fmt}"))
    if not frames:
        raise RuntimeError("超分输出帧为空")

    fps_s = f"{max(1.0, fps):.3f}"
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            fps_s,
            "-i",
            str(pattern),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
    )
    if proc.returncode != 0 or not dest.is_file():
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise RuntimeError(f"合帧失败: {tail}")


def run_ncnn_video_sr(
    src: Path,
    dest: Path,
    *,
    scale: int,
    model: str | None = None,
    src_w: int = 0,
    src_h: int = 0,
    on_progress: ProgressFn | None = None,
) -> None:
    """
    ncnn-vulkan 仅支持图片/目录：抽帧 → 目录超分 → 再合成 mp4。
    """
    if not src.is_file():
        raise RuntimeError("超分输入视频不存在")
    fmt = (os.getenv("HONGGUO_REALESRGAN_FRAME_FMT", "jpg").strip() or "jpg").lower()
    if fmt not in ("jpg", "png", "webp"):
        fmt = "jpg"
    tile = _env_int("HONGGUO_REALESRGAN_TILE", 0)
    exe = ensure_ncnn_bundle()
    exe_parent = exe.parent
    models_dir = exe_parent / "models"
    model_name = _resolve_model_name(model or "", models_dir)
    scale = _ncnn_scale(model_name, scale)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    phase_start = time.monotonic()
    ai_phase_start: float | None = None

    def report(
        msg: str,
        frac: float,
        *,
        frames_done: int = 0,
        frames_total: int = 0,
    ) -> None:
        if not on_progress:
            return
        frac = max(0.0, min(1.0, frac))
        eta_sec: int | None = None
        if frames_total > 0 and frames_done > 0:
            base = ai_phase_start if ai_phase_start is not None else phase_start
            eta_sec = _eta_from_frames(time.monotonic() - base, frames_done, frames_total)
        else:
            eta_sec = _eta_from_frac(time.monotonic() - phase_start, frac)
        on_progress(msg, frac, eta_sec)

    jobs_root = exe_parent / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    work = jobs_root / f"sr_{os.getpid()}_{int(time.time() * 1000)}"
    in_frames = work / "in"
    out_frames = work / "out"
    shutil.rmtree(work, ignore_errors=True)
    in_frames.mkdir(parents=True, exist_ok=True)
    out_frames.mkdir(parents=True, exist_ok=True)

    in_rel = f"jobs/{work.name}/in"
    out_rel = f"jobs/{work.name}/out"

    try:
        max_long = _sr_max_input_long_edge()
        prep_vf = None
        prep_dims = None
        if src_w > 1 and src_h > 1:
            prep_dims = _prep_scale_dims(src_w, src_h, max_long)
            if prep_dims:
                prep_vf = f"scale={prep_dims[0]}:{prep_dims[1]}:flags=lanczos"
                report(
                    f"正在抽帧并缩至 {prep_dims[0]}×{prep_dims[1]}（最长边 {max_long}px）…",
                    0.02,
                )
        else:
            report("正在抽帧…", 0.02)
        src_fps = _extract_frames(src, in_frames, fmt=fmt, vf=prep_vf)
        in_count = len(list(in_frames.glob(f"*.{fmt}")))
        if in_count < 1:
            raise RuntimeError("抽帧失败：未得到任何画面，无法超分")
        prep_hint = f"（{prep_dims[0]}×{prep_dims[1]}）" if prep_dims else ""
        report(f"抽帧完成，共 {in_count} 帧{prep_hint}", 0.05)

        threads = (os.getenv("HONGGUO_REALESRGAN_THREADS", "4:4:4") or "").strip()
        cmd = [
            str(exe),
            "-i",
            in_rel,
            "-o",
            out_rel,
            "-m",
            "models",
            "-n",
            model_name,
            "-s",
            str(scale),
            "-f",
            fmt,
        ]
        if tile > 0:
            cmd.extend(["-t", str(tile)])
        if threads:
            cmd.extend(["-j", threads])

        logger.info(
            "Real-ESRGAN v%d 超分 %s（%d 帧）→ %sx 模型 %s prep_long<=%s threads=%s cwd=%s",
            SR_PIPELINE_VERSION,
            src.name,
            in_count,
            scale,
            model_name,
            max_long if prep_vf else "full",
            threads or "default",
            exe_parent,
        )
        ai_phase_start = time.monotonic()
        timeout = _env_int("HONGGUO_REALESRGAN_TIMEOUT", 7200)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(exe_parent),
        )
        started = time.monotonic()
        last_report = 0.0
        while proc.poll() is None:
            if time.monotonic() - started > timeout:
                proc.kill()
                raise RuntimeError(f"Real-ESRGAN 超分超时（>{timeout}s）")
            out_count = len(list(out_frames.glob(f"*.{fmt}")))
            if in_count > 0 and time.monotonic() - last_report >= 1.0:
                frac = 0.05 + 0.82 * min(1.0, out_count / in_count)
                report(
                    f"AI 超分 {out_count}/{in_count} 帧",
                    frac,
                    frames_done=out_count,
                    frames_total=in_count,
                )
                last_report = time.monotonic()
            time.sleep(0.4)

        stdout, stderr = proc.communicate(timeout=30)
        out_count = len(list(out_frames.glob(f"*.{fmt}")))
        combined = "\n".join(
            x for x in ((stderr or "").strip(), (stdout or "").strip()) if x
        )
        if proc.returncode != 0:
            tail = combined[-800:]
            if out_count >= in_count and out_count > 0:
                logger.warning(
                    "Real-ESRGAN 退出码 %s 但已输出 %d/%d 帧，继续合成",
                    proc.returncode,
                    out_count,
                    in_count,
                )
            elif "%" in combined and "invalid outputpath" not in combined.lower():
                raise RuntimeError(
                    "Real-ESRGAN 超分被中断（进度约 "
                    f"{out_count}/{in_count} 帧）。"
                    "AI 超分耗时很长，请勿在任务进行中重启服务；"
                    "请用不带 --reload 的方式启动："
                    "py -3.12 -m uvicorn main:app --host 0.0.0.0 --port 8000"
                )
            else:
                hint = ""
                if "invalid outputpath" in tail.lower():
                    hint = (
                        f"（请确认 server/video_enhance.py 已为 v{SR_PIPELINE_VERSION} 抽帧流程）"
                    )
                raise RuntimeError(
                    f"Real-ESRGAN 超分失败: {tail or '进程异常退出'}{hint}"
                )

        if out_count < 1:
            raise RuntimeError("Real-ESRGAN 未输出任何超分帧")

        report("正在合成超分视频…", 0.9)
        _merge_frames(out_frames, dest, fmt=fmt, fps=src_fps)
        report("超分视频合成完成", 0.95)
        if not dest.is_file() or dest.stat().st_size < 10_000:
            raise RuntimeError("超分后视频为空")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # 清理空 jobs 目录
        try:
            if jobs_root.is_dir() and not any(jobs_root.iterdir()):
                jobs_root.rmdir()
        except OSError:
            pass


def _probe_size(path: Path) -> tuple[int, int]:
    ffmpeg = resolve_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        m = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def mux_video(
    video: Path,
    audio_src: Path,
    dest: Path,
    *,
    vf: str | None = None,
    fps: int | None = None,
) -> None:
    """合成视频轨 + 可选滤镜/帧率 + 音频。"""
    ffmpeg = resolve_ffmpeg_exe()
    crf = os.getenv("HONGGUO_WM_CRF", "18").strip() or "18"
    preset = os.getenv("HONGGUO_WM_ENCODE_PRESET", "medium").strip() or "medium"
    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio_src),
    ]
    if vf:
        args.extend(["-vf", vf])
    args.extend(
        [
            "-map",
            "0:v",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if fps and fps > 0:
        args.extend(["-r", str(fps)])
    args.extend(["-c:a", "copy", "-movflags", "+faststart", str(dest)])
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=2400,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"视频合成失败: {tail}")


def apply_sr_output(
    video: Path,
    audio_src: Path,
    dest: Path,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    fps: int,
    on_progress: ProgressFn | None = None,
) -> str:
    """
    Real-ESRGAN 超分 → 精确缩放到目标分辨率 → 插帧 → 混音。
    返回使用的后端名称。
    """
    scale = calc_sr_scale(src_w, src_h, out_w, out_h)
    sr_out = dest.with_suffix(".sr.mp4")
    try:
        run_ncnn_video_sr(
            video,
            sr_out,
            scale=scale,
            src_w=src_w,
            src_h=src_h,
            on_progress=on_progress,
        )
        sr_w, sr_h = _probe_size(sr_out)
        if sr_w < 2 or sr_h < 2:
            sr_w, sr_h = src_w * scale, src_h * scale

        vf_parts: list[str] = []
        if sr_w != out_w or sr_h != out_h:
            vf_parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
        if fps > 0:
            vf_parts.append(f"fps={fps}")
        vf = ",".join(vf_parts) if vf_parts else None
        if on_progress:
            on_progress("正在缩放并合并音轨…", 0.97, None)
        mux_video(sr_out, audio_src, dest, vf=vf, fps=None)
        if on_progress:
            on_progress("超分处理完成", 1.0, None)
        return f"realesrgan-x{scale}"
    finally:
        sr_out.unlink(missing_ok=True)


def super_resolve_video(
    src: Path,
    dest: Path,
    *,
    output_scale: str = "1080",
    output_fps_choice: str | int | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, int | str]:
    """仅 AI 超分（不去水印）：Real-ESRGAN → 缩放到目标分辨率 → 保留原声。"""
    if not src.is_file():
        raise RuntimeError("源视频不存在")
    if src.stat().st_size < 10_000:
        raise RuntimeError("视频文件过小或已损坏")
    if not sr_available():
        raise RuntimeError(
            "Real-ESRGAN 未就绪，请先在 Mac 上运行 ./start.sh（首次会自动下载工具）"
        )
    ensure_ncnn_bundle()

    from watermark_remove import (
        normalize_output_fps_choice,
        normalize_output_scale,
        probe_video_fps,
        probe_video_size,
        resolve_wm_output_spec,
    )

    src_w, src_h = probe_video_size(src)
    scale_key = normalize_output_scale(output_scale)
    out_spec = resolve_wm_output_spec(src_w, src_h, scale_key)

    fps_choice = normalize_output_fps_choice(output_fps_choice)
    if fps_choice == -1:
        fps = max(1, min(120, int(round(probe_video_fps(src))) or 30))
    elif fps_choice > 0:
        fps = fps_choice
    else:
        src_fps = probe_video_fps(src)
        fps = max(1, min(120, int(round(src_fps)))) if src_fps >= 1 else 30

    job_start = time.monotonic()

    def relay_progress(msg: str, frac: float, eta_sec: int | None = None) -> None:
        if not on_progress:
            return
        if eta_sec is None:
            eta_sec = _eta_from_frac(time.monotonic() - job_start, frac)
        on_progress(msg, frac, eta_sec)

    relay_progress("正在准备超分…", 0.01, None)

    backend = apply_sr_output(
        src,
        src,
        dest,
        src_w=src_w,
        src_h=src_h,
        out_w=out_spec.width,
        out_h=out_spec.height,
        fps=fps,
        on_progress=relay_progress,
    )
    return {
        "output_width": out_spec.width,
        "output_height": out_spec.height,
        "output_fps": fps,
        "enhance_backend": backend,
        "source_width": src_w,
        "source_height": src_h,
        "enhance": "sr",
    }
