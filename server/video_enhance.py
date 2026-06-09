"""Real-ESRGAN 视频超分（ncnn-vulkan：抽帧 → 超分 → 合成）。"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx

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


def find_ncnn_exe() -> Path | None:
    bundled = TOOLS_DIR / _ncnn_exe_name()
    if bundled.is_file():
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

    exe = find_ncnn_exe()
    if exe is None:
        for candidate in TOOLS_DIR.rglob(_ncnn_exe_name()):
            if candidate.is_file():
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
    raw = (model or os.getenv("HONGGUO_REALESRGAN_MODEL", "realesrgan-x4plus")).strip()
    name = _MODEL_ALIASES.get(raw, raw)
    candidates = [name, "realesrgan-x4plus", "realesr-animevideov3", "realesrgan-x4plus-anime"]
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


def _extract_frames(src: Path, frames_dir: Path, *, fmt: str) -> float:
    frames_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg_exe()
    pattern = frames_dir / f"frame%08d.{fmt}"
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-qscale:v",
            "1",
            "-qmin",
            "1",
            "-qmax",
            "1",
            "-vsync",
            "0",
            str(pattern),
        ],
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

    import time

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
        src_fps = _extract_frames(src, in_frames, fmt=fmt)
        in_count = len(list(in_frames.glob(f"*.{fmt}")))
        if in_count < 1:
            raise RuntimeError("抽帧失败：未得到任何画面，无法超分")

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

        logger.info(
            "Real-ESRGAN v%d 超分 %s（%d 帧）→ %sx 模型 %s cwd=%s",
            SR_PIPELINE_VERSION,
            src.name,
            in_count,
            scale,
            model_name,
            exe_parent,
        )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_env_int("HONGGUO_REALESRGAN_TIMEOUT", 7200),
            cwd=str(exe_parent),
        )
        out_count = len(list(out_frames.glob(f"*.{fmt}")))
        combined = "\n".join(
            x for x in ((proc.stderr or "").strip(), (proc.stdout or "").strip()) if x
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

        _merge_frames(out_frames, dest, fmt=fmt, fps=src_fps)
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
) -> str:
    """
    Real-ESRGAN 超分 → 精确缩放到目标分辨率 → 插帧 → 混音。
    返回使用的后端名称。
    """
    scale = calc_sr_scale(src_w, src_h, out_w, out_h)
    sr_out = dest.with_suffix(".sr.mp4")
    try:
        run_ncnn_video_sr(video, sr_out, scale=scale)
        sr_w, sr_h = _probe_size(sr_out)
        if sr_w < 2 or sr_h < 2:
            sr_w, sr_h = src_w * scale, src_h * scale

        vf_parts: list[str] = []
        if sr_w != out_w or sr_h != out_h:
            vf_parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
        if fps > 0:
            vf_parts.append(f"fps={fps}")
        vf = ",".join(vf_parts) if vf_parts else None
        mux_video(sr_out, audio_src, dest, vf=vf, fps=None)
        return f"realesrgan-x{scale}"
    finally:
        sr_out.unlink(missing_ok=True)
