"""本地视频去水印（默认小区域 delogo；可选半透明模糊覆盖）。"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Literal

StrengthPreset = Literal["tight", "normal", "strong"]

from ffmpeg_util import resolve_ffmpeg_exe, resolve_ffprobe_exe

logger = logging.getLogger(__name__)

_delogo_caps_cache: dict[str, bool] | None = None

# 单次去水印任务输出（线程内 contextvars，避免并发互相覆盖）
_wm_job_scale: contextvars.ContextVar[str] = contextvars.ContextVar("wm_job_scale", default="")
_wm_job_fps: contextvars.ContextVar[int] = contextvars.ContextVar(
    "wm_job_fps", default=-2
)  # -2=未设用环境变量, -1=跟原片, >0=指定帧率
_wm_source_fps: contextvars.ContextVar[float] = contextvars.ContextVar("wm_source_fps", default=0.0)
_wm_job_enhance: contextvars.ContextVar[str] = contextvars.ContextVar("wm_job_enhance", default="")

PORTRAIT_4K = (2160, 3840)
LANDSCAPE_4K = (3840, 2160)

PositionPreset = Literal[
    "douyin",
    "doubao",
    "bottom-right",
    "bottom-left",
    "top-right",
    "top-left",
    "bottom-bar",
    "all-corners",
]

PRESET_LABELS: dict[str, str] = {
    "douyin": "抖音角标（仅右下小条，推荐）",
    "doubao": "豆包 AI（右下 + 左上，位置不固定时推荐）",
    "bottom-right": "仅右下角",
    "bottom-left": "仅左下角",
    "top-right": "仅右上角",
    "top-left": "仅左上角",
    "bottom-bar": "底部横条（全宽字幕/水印条）",
    "all-corners": "四角都去（体积略大、略慢）",
}


def _even(n: int) -> int:
    return max(2, n - (n % 2))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class _WmProfile:
    scale_w: float
    scale_h: float
    pad: int
    blur_radius: int
    blur_power: int
    blur_sigma: float
    feather_px: int
    overlay_alpha: float
    delogo_band: int


_WM_PROFILES: dict[str, _WmProfile] = {
    "tight": _WmProfile(0.20, 0.07, 4, 10, 5, 6.0, 0, 0.52, 18),
    "normal": _WmProfile(0.24, 0.09, 6, 14, 7, 8.0, 0, 0.72, 24),
    "strong": _WmProfile(0.32, 0.13, 10, 18, 9, 10.0, 0, 0.92, 32),
}


def wm_strength(value: str | None = None) -> str:
    s = (value or os.getenv("HONGGUO_WM_STRENGTH", "normal")).strip().lower()
    return s if s in _WM_PROFILES else "normal"


def _profile(strength: str | None = None) -> _WmProfile:
    return _WM_PROFILES[wm_strength(strength)]


def _legacy_delogo() -> bool:
    """imageio 自带 FFmpeg 4.2 等无 delogo band，逐帧插值易闪烁。"""
    return not _delogo_capabilities().get("band")


def wm_method(explicit: str | None = None) -> str:
    """
    inpaint=OpenCV 逐帧修复（默认，需 opencv-python-headless）；
    blur_cover / delogo=FFmpeg 快速方案。
    """
    v = (explicit or os.getenv("HONGGUO_WM_METHOD", "")).strip().lower()
    if v in ("blur_cover", "delogo", "inpaint", "fast", "best"):
        if v == "fast":
            return "blur_cover"
        if v == "best":
            return "inpaint"
        return v
    try:
        from watermark_inpaint import opencv_available

        if opencv_available():
            return "inpaint"
    except ImportError:
        pass
    if _legacy_delogo():
        return "blur_cover"
    return "delogo"


def opencv_inpaint_available() -> bool:
    try:
        from watermark_inpaint import opencv_available

        return opencv_available()
    except ImportError:
        return False


def _use_tmix() -> bool:
    if not _env_int("HONGGUO_WM_DEFLICKER", 1):
        return False
    explicit = os.getenv("HONGGUO_WM_TMIX", "").strip()
    if explicit in ("0", "false", "no"):
        return False
    if explicit in ("1", "true", "yes"):
        return True
    # 默认关闭：tmix 易在角标处留下拖影/残影
    return False


def _tmix_vf() -> str:
    frames = max(3, min(7, _env_int("HONGGUO_WM_TMIX_FRAMES", 3)))
    weights = os.getenv("HONGGUO_WM_TMIX_WEIGHTS", "1 2 1").strip() or "1 2 1"
    return f"tmix=frames={frames}:weights='{weights}'"


def _hqdn3d_vf(*, for_delogo: bool) -> str:
    if _legacy_delogo():
        luma = _env_float("HONGGUO_WM_HQDN3D_LUMA", 2.2 if for_delogo else 1.4)
        chroma = _env_float("HONGGUO_WM_HQDN3D_CHROMA", 1.6 if for_delogo else 1.0)
        tc = _env_int("HONGGUO_WM_HQDN3D_TC", 5)
        cc = _env_int("HONGGUO_WM_HQDN3D_CC", 5)
    else:
        luma = _env_float("HONGGUO_WM_HQDN3D_LUMA", 1.2 if for_delogo else 0.9)
        chroma = _env_float("HONGGUO_WM_HQDN3D_CHROMA", 0.0)
        tc = _env_int("HONGGUO_WM_HQDN3D_TC", 4)
        cc = _env_int("HONGGUO_WM_HQDN3D_CC", 3)
    return f"hqdn3d={luma}:{chroma}:{tc}:{cc}"


def _parse_fps_value(raw: str) -> float:
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    if "/" in raw:
        num, den = raw.split("/", 1)
        try:
            d = float(den)
            return float(num) / d if d > 0 else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def probe_video_fps(path: Path) -> float:
    """读取源片帧率；失败时回退 30。"""
    ffprobe = resolve_ffprobe_exe()
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
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode == 0 and proc.stdout:
                data = json.loads(proc.stdout)
                streams = data.get("streams") or []
                if streams:
                    st = streams[0]
                    for key in ("avg_frame_rate", "r_frame_rate"):
                        fps = _parse_fps_value(str(st.get(key) or ""))
                        if fps >= 1:
                            return fps
        except (ValueError, OSError, json.JSONDecodeError):
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


def normalize_output_scale(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in ("native", "source", "原画", "0", "original"):
        return "native"
    if v in ("4k", "2160", "uhd", "4khd"):
        return "4k"
    if v in ("1080", "1080p", "standard", "hd", ""):
        return "1080"
    return "1080"


def normalize_output_fps_choice(value: str | int | None) -> int:
    """
    返回 context 用的帧率选择：
    -2 未指定（读环境变量）
    -1 跟原片
    >0 目标帧率（上限 120）
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return -2
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("native", "source", "原片", "0", "original"):
            return -1
        try:
            n = int(v)
        except ValueError:
            return -2
        if n <= 0:
            return -1
        return max(1, min(120, n))
    try:
        n = int(value)
    except (TypeError, ValueError):
        return -2
    if n <= 0:
        return -1
    return max(1, min(120, n))


def resolve_wm_output_spec(src_w: int, src_h: int, scale: str):
    from output_canvas import CanvasSpec, PORTRAIT_SIZE, LANDSCAPE_SIZE, _label_for

    portrait = src_h > src_w * 1.05
    key = normalize_output_scale(scale)
    if key == "native":
        w, h = _even(src_w), _even(src_h)
        return CanvasSpec(w, h, _label_for(w, h))
    if key == "4k":
        w, h = (PORTRAIT_4K if portrait else LANDSCAPE_4K)
        return CanvasSpec(_even(w), _even(h), f"{'9:16' if portrait else '16:9'} 4K")
    w, h = (PORTRAIT_SIZE if portrait else LANDSCAPE_SIZE)
    return CanvasSpec(w, h, f"{'9:16' if portrait else '16:9'}")


def output_fps() -> int:
    job_fps = _wm_job_fps.get()
    if job_fps == -1:
        src_fps = _wm_source_fps.get()
        if src_fps >= 1:
            return max(1, min(120, int(round(src_fps))))
        return 30
    if job_fps >= 1:
        return min(120, job_fps)
    try:
        raw = os.getenv("HONGGUO_WM_OUTPUT_FPS") or os.getenv("HONGGUO_OUTPUT_FPS", "60")
        return max(1, min(120, int(raw or 60)))
    except ValueError:
        return 60


def _fps_vf() -> str:
    return f"fps={output_fps()}"


def _output_canvas_spec(src_w: int, src_h: int):
    job_scale = _wm_job_scale.get()
    if job_scale:
        return resolve_wm_output_spec(src_w, src_h, job_scale)

    from output_canvas import canvas_from_source_size

    w_env = os.getenv("HONGGUO_WM_OUTPUT_WIDTH", "").strip()
    h_env = os.getenv("HONGGUO_WM_OUTPUT_HEIGHT", "").strip()
    if w_env and h_env:
        from output_canvas import CanvasSpec, _label_for

        w, h = _even(int(w_env)), _even(int(h_env))
        return CanvasSpec(w, h, _label_for(w, h))
    return canvas_from_source_size(src_w, src_h)


def _scale_vf(src_w: int, src_h: int) -> str:
    spec = _output_canvas_spec(src_w, src_h)
    if spec.width == src_w and spec.height == src_h:
        return ""
    return f"scale={spec.width}:{spec.height}:flags=lanczos"


def _scale_before_wm() -> bool:
    return _env_int("HONGGUO_WM_SCALE_FIRST", 1) != 0


def _scale_rects(
    rects: list[tuple[int, int, int, int]],
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> list[tuple[int, int, int, int]]:
    if src_w == out_w and src_h == out_h:
        return rects
    sx = out_w / src_w
    sy = out_h / src_h
    scaled: list[tuple[int, int, int, int]] = []
    for x, y, w, h in rects:
        scaled.append(
            (
                _even(int(round(x * sx))),
                _even(int(round(y * sy))),
                _even(int(round(w * sx))),
                _even(int(round(h * sy))),
            )
        )
    return scaled


def _expand_for_feather(
    vw: int,
    vh: int,
    x: int,
    y: int,
    w: int,
    h: int,
    feather: int,
) -> tuple[int, int, int, int]:
    f = max(0, feather)
    ex = max(0, x - f)
    ey = max(0, y - f)
    ew = min(vw - ex, w + 2 * f)
    eh = min(vh - ey, h + 2 * f)
    return _even(ex), _even(ey), _even(ew), _even(eh)


def _feather_geq(feather: int) -> str:
    f = max(2, feather)
    return (
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='if(lt(X,{f}),255*X/{f},if(lt(Y,{f}),255*Y/{f},"
        f"if(gt(X,W-{f}),255*(W-X)/{f},if(gt(Y,H-{f}),255*(H-Y)/{f},255))))'"
    )


def probe_duration(path: Path) -> float:
    ffprobe = resolve_ffprobe_exe()
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return max(0.0, float((proc.stdout or "").strip()))
        except (ValueError, OSError):
            pass
    ffmpeg = resolve_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def _preset_has_top_left(preset: str) -> bool:
    p = (preset or "").strip().lower()
    return p in ("doubao", "top-left", "all-corners")


def _find_top_left_rect(
    rects: list[tuple[int, int, int, int]],
    *,
    frame_h: int,
) -> tuple[int, int, int, int] | None:
    """取 y 最小且靠左的遮罩（豆包左上）。"""
    candidates = [r for r in rects if r[1] < frame_h * 0.35]
    if not candidates:
        return None
    return min(candidates, key=lambda r: (r[1], r[0]))


def _merge_time_segments(
    segments: list[tuple[float, float]],
    *,
    gap: float = 0.45,
) -> list[tuple[float, float]]:
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s[0])
    merged: list[tuple[float, float]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_s, prev_e = merged[-1]
        if start <= prev_e + gap:
            merged[-1] = (prev_s, max(prev_e, end))
        else:
            merged.append((start, end))
    return merged


def _pad_time_segments(
    segments: list[tuple[float, float]],
    duration: float,
    *,
    pad: float = 0.25,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in segments:
        s = max(0.0, start - pad)
        e = min(duration, end + pad)
        if e - s >= 0.08:
            out.append((s, e))
    return _merge_time_segments(out, gap=0.0)


def _enable_expr_for_segments(segments: list[tuple[float, float]]) -> str:
    parts = [f"between(t,{s:.3f},{e:.3f})" for s, e in segments]
    return parts[0] if len(parts) == 1 else "+".join(parts)


def _sample_tl_bright_ratio(
    src: Path,
    t: float,
    *,
    crop_w: int,
    crop_h: int,
) -> float:
    ffmpeg = resolve_ffmpeg_exe()
    raw_path = Path(tempfile.gettempdir()) / f"wm_tl_{os.getpid()}_{int(t * 1000)}.raw"
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, t):.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                f"crop={crop_w}:{crop_h}:0:0,format=gray",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                str(raw_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0 or not raw_path.is_file():
            return 0.0
        data = raw_path.read_bytes()
        if not data:
            return 0.0
        bright = sum(1 for b in data if b > 175)
        return bright / len(data)
    finally:
        raw_path.unlink(missing_ok=True)


def detect_top_left_watermark_segments(
    src: Path,
    vw: int,
    vh: int,
    *,
    duration: float | None = None,
) -> list[tuple[float, float]]:
    """
    扫描左上角，返回有明显亮角标（豆包等）的时间段，供分段精修。
    """
    if _env_int("HONGGUO_WM_TL_DETECT", 1) == 0:
        return []

    dur = duration if duration is not None else probe_duration(src)
    if dur < 0.2:
        return []

    step = _env_float("HONGGUO_WM_TL_DETECT_STEP", 0.25)
    thresh = _env_float("HONGGUO_WM_TL_DETECT_THRESH", 0.04)
    cw = _even(min(vw, int(vw * 0.24)))
    ch = _even(min(vh, int(vh * 0.11)))

    hits: list[tuple[float, float]] = []
    t = 0.0
    while t < dur:
        if _sample_tl_bright_ratio(src, t, crop_w=cw, crop_h=ch) >= thresh:
            hits.append((t, min(dur, t + step)))
        t += step

    segments = _merge_time_segments(hits)
    return _pad_time_segments(segments, dur, pad=_env_float("HONGGUO_WM_TL_REFINE_PAD", 0.25))


def _tl_refine_enabled(preset: str) -> bool:
    if _env_int("HONGGUO_WM_TL_REFINE", 1) == 0:
        return False
    return _preset_has_top_left(preset)


def _refine_top_left_timed(
    video: Path,
    rect: tuple[int, int, int, int],
    segments: list[tuple[float, float]],
    profile: _WmProfile,
    *,
    vw: int,
    vh: int,
) -> None:
    if not segments:
        return

    x, y, w, h = rect
    ex, ey, ew, eh = _expand_rect(vw, vh, (x, y, w, h), pad=_env_int("HONGGUO_WM_TL_REFINE_PAD_PX", 10))
    sigma = min(
        14.0,
        _gblur_sigma(profile) * _env_float("HONGGUO_WM_TL_REFINE_SIGMA_MUL", 1.4),
    )
    bb = max(12, int(sigma))
    bp = max(6, int(sigma // 2))
    enable = _enable_expr_for_segments(segments)

    fc = (
        f"[0:v]split=2[btl][stl];"
        f"[stl]crop={ew}:{eh}:{ex}:{ey},gblur=sigma={sigma:.1f}:steps=1,"
        f"boxblur={bb}:{bp}[ptl];"
        f"[btl][ptl]overlay={ex}:{ey}:enable='{enable}'[vout]"
    )

    tmp = video.with_suffix(".tlrefine.mp4")
    _run_ffmpeg(video, tmp, filter_complex=fc, map_video="[vout]", apply_output_fps=False)
    tmp.replace(video)
    logger.info(
        "左上分段精修 %s：%d 段 %s",
        video.name,
        len(segments),
        ", ".join(f"{s:.1f}-{e:.1f}s" for s, e in segments[:6]),
    )


def _gblur_sigma(profile: _WmProfile) -> float:
    raw = os.getenv("HONGGUO_WM_BLUR_SIGMA", "").strip()
    if raw:
        return max(1.0, min(12.0, float(raw)))
    return profile.blur_sigma


def _wm_x264_params() -> str:
    fps = output_fps()
    default = f"keyint={fps * 2}:min-keyint={fps}:scenecut=0:bf=0"
    return os.getenv("HONGGUO_WM_X264_PARAMS", default).strip() or default


def _overlay_alpha(profile: _WmProfile) -> float:
    raw = os.getenv("HONGGUO_WM_OVERLAY_ALPHA", "").strip()
    if raw:
        return max(0.35, min(1.0, float(raw)))
    if _legacy_delogo():
        return 1.0
    return max(0.35, min(1.0, profile.overlay_alpha))


def probe_video_size(path: Path) -> tuple[int, int]:
    ffprobe = resolve_ffprobe_exe()
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
                    "stream=width,height",
                    "-of",
                    "csv=p=0:s=x",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                line = (proc.stdout or "").strip().splitlines()[0]
                w, h = line.split("x")
                return int(w), int(h)
        except (ValueError, OSError, IndexError) as exc:
            logger.debug("ffprobe size: %s", exc)

    ffmpeg = resolve_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        m = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if m:
            return int(m.group(1)), int(m.group(2))
    raise RuntimeError("无法读取视频分辨率")


def _corner_rect(
    width: int,
    height: int,
    corner: str,
    *,
    margin_pct: float = 0.02,
    scale_w: float = 0.28,
    scale_h: float = 0.11,
) -> tuple[int, int, int, int]:
    mx = int(width * margin_pct)
    my = int(height * margin_pct)
    w = _even(int(width * scale_w))
    h = _even(int(height * scale_h))
    c = corner.strip().lower()
    if c == "bottom-left":
        return _even(mx), _even(height - h - my), w, h
    if c == "top-right":
        return _even(width - w - mx), _even(my), w, h
    if c == "top-left":
        return _even(mx), _even(my), w, h
    # 底角贴底对齐，避免「豆包AI生成」下半截露在遮罩外
    return _even(width - w - mx), _even(height - h - my), w, h


def _doubao_scales(profile: _WmProfile) -> tuple[float, float]:
    sw = _env_float("HONGGUO_WM_DOUBAO_SCALE_W", profile.scale_w)
    sh = _env_float("HONGGUO_WM_DOUBAO_SCALE_H", profile.scale_h)
    return max(0.12, min(0.38, sw)), max(0.05, min(0.18, sh))


def _douyin_scales(strength: str | None = None) -> tuple[float, float]:
    """抖音角标多为右下小条（OCR 常识别不到图形 logo）。"""
    key = wm_strength(strength)
    table = {
        # 右下水印（你这类截图）通常“很靠右、很靠下、宽但不高”
        # 需要给宽度足够空间，否则会出现“漏掉一截”的漏洞。
        "tight": (0.26, 0.038),
        "normal": (0.28, 0.042),
        "strong": (0.30, 0.048),
    }
    sw, sh = table.get(key, table["normal"])
    sw = _env_float("HONGGUO_WM_DOUYIN_SCALE_W", sw)
    sh = _env_float("HONGGUO_WM_DOUYIN_SCALE_H", sh)
    return max(0.10, min(0.45, sw)), max(0.03, min(0.14, sh))


def _scales_for_corner(
    corner: str,
    profile: _WmProfile,
    *,
    preset: str = "",
) -> tuple[float, float]:
    """豆包预设才放大底角；普通「仅右下角」用 profile 默认比例。"""
    c = corner.strip().lower()
    p = (preset or "").strip().lower()
    sw, sh = profile.scale_w, profile.scale_h
    if p == "doubao":
        if c == "top-left":
            sw = max(sw, _env_float("HONGGUO_WM_TOPLEFT_SCALE_W", 0.28))
            sh = max(sh, _env_float("HONGGUO_WM_TOPLEFT_SCALE_H", 0.075))
        elif c in ("bottom-right", "bottom-left"):
            sw = max(sw, _env_float("HONGGUO_WM_BOTTOM_SCALE_W", 0.42))
            sh = max(sh, _env_float("HONGGUO_WM_BOTTOM_SCALE_H", 0.145))
    return sw, sh


def _expand_rect(
    vw: int,
    vh: int,
    rect: tuple[int, int, int, int],
    *,
    pad: int,
) -> tuple[int, int, int, int]:
    """略扩大遮罩，避免水印边缘残留导致逐帧闪烁。"""
    x, y, w, h = rect
    p = max(0, pad)
    x = max(0, x - p)
    y = max(0, y - p)
    w = min(vw - x, w + 2 * p)
    h = min(vh - y, h + 2 * p)
    return _even(x), _even(y), _even(w), _even(h)


def refine_douyin_rects(
    frame,
    rects: list[tuple[int, int, int, int]],
    *,
    pad: int = 8,
) -> list[tuple[int, int, int, int]]:
    """根据右下白色角标（小云雀AI 等）自动框选，必须盖住整段白字。"""
    import cv2
    import numpy as np

    if frame is None or not rects:
        return rects

    vh, vw = frame.shape[:2]
    zsy = max(0, int(vh * 0.91))
    zsx = max(0, int(vw * 0.72))
    zone = frame[zsy:vh, zsx:vw]
    if zone.size < 64:
        return rects

    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 198, 255, cv2.THRESH_BINARY)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 3))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, k, iterations=2)
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return rects

    pick: tuple[int, int, int, int] | None = None
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw * bh < 100 or bw < 40 or bh < 8 or bh > 80:
            continue
        gx = zsx + bx
        if gx < int(vw * 0.68):
            continue
        if pick is None or gx + bw > pick[0]:
            pick = (gx + bw, bx, by, bw, bh)

    if pick is None:
        return rects

    _, bx, by, bw, bh = pick
    nx = max(int(vw * 0.68), zsx + bx - pad)
    ny = max(0, zsy + by - pad)
    x2 = min(vw, zsx + bx + bw + pad)
    y2 = vh
    nw = min(max(48, x2 - nx), int(vw * 0.22))
    nh = max(18, y2 - ny)
    return [(nx, ny, nw, nh)]


def rects_for_preset_geometric(
    width: int,
    height: int,
    preset: str,
    *,
    margin_pct: float = 0.02,
    strength: str | None = None,
) -> list[tuple[int, int, int, int]]:
    p = (preset or "bottom-right").strip().lower()
    prof = _profile(strength)
    pad = _env_int("HONGGUO_WM_PAD", prof.pad)
    sw, sh = prof.scale_w, prof.scale_h

    if p == "bottom-bar":
        bar_h = { "tight": 0.08, "normal": 0.10, "strong": 0.12 }.get(wm_strength(strength), 0.08)
        h = _even(int(height * bar_h))
        y = _even(height - h - int(height * margin_pct))
        raw = (0, y, _even(width), h)
        return [_expand_rect(width, height, raw, pad=pad)]

    if p == "douyin":
        dsw, dsh = _douyin_scales(strength)
        raw = _corner_rect(
            width, height, "bottom-right", margin_pct=0.0, scale_w=dsw, scale_h=dsh
        )
        x, y, rw, rh = raw
        right_gap = _env_int("HONGGUO_WM_DOUYIN_RIGHT_MARGIN", 4)
        y = _even(max(0, height - rh))
        x = _even(max(0, width - rw - right_gap))
        raw = (x, y, rw, rh)
        dy_pad = min(pad, _env_int("HONGGUO_WM_DOUYIN_PAD", 6))
        return [_expand_rect(width, height, raw, pad=dy_pad)]

    if p == "doubao":
        br_sw, br_sh = _scales_for_corner("bottom-right", prof, preset=p)
        tl_sw, tl_sh = _scales_for_corner("top-left", prof, preset=p)
        raw = [
            _corner_rect(
                width, height, "bottom-right", margin_pct=margin_pct, scale_w=br_sw, scale_h=br_sh
            ),
            _corner_rect(
                width, height, "top-left", margin_pct=margin_pct, scale_w=tl_sw, scale_h=tl_sh
            ),
        ]
        return [_expand_rect(width, height, r, pad=pad) for r in raw]

    if p == "all-corners":
        corners = ("top-left", "top-right", "bottom-left", "bottom-right")
        raw = []
        for c in corners:
            csw, csh = _scales_for_corner(c, prof, preset=p)
            raw.append(
                _corner_rect(width, height, c, margin_pct=margin_pct, scale_w=csw, scale_h=csh)
            )
        return [_expand_rect(width, height, r, pad=pad) for r in raw]

    csw, csh = _scales_for_corner(p, prof, preset=p)
    raw = _corner_rect(width, height, p, margin_pct=margin_pct, scale_w=csw, scale_h=csh)
    return [_expand_rect(width, height, raw, pad=pad)]


def rects_for_preset(
    width: int,
    height: int,
    preset: str,
    *,
    src: Path | None = None,
    margin_pct: float = 0.02,
    strength: str | None = None,
) -> list[tuple[int, int, int, int]]:
    """标配：有视频路径时用本地 OCR 自动框，失败则回退几何预设。"""
    if src is not None and src.is_file():
        from watermark_ocr import rects_from_ocr_video

        dur = probe_duration(src)
        return rects_from_ocr_video(
            src,
            width,
            height,
            preset,
            duration=dur,
            strength=strength,
            geometric_fallback=rects_for_preset_geometric,
        )
    return rects_for_preset_geometric(
        width, height, preset, margin_pct=margin_pct, strength=strength
    )


def rect_from_custom(
    width: int,
    height: int,
    x: int,
    y: int,
    w: int,
    h: int,
) -> tuple[int, int, int, int]:
    x = max(0, min(x, width - 4))
    y = max(0, min(y, height - 4))
    w = max(4, min(w, width - x))
    h = max(4, min(h, height - y))
    pad = _env_int("HONGGUO_WM_PAD", _profile().pad)
    return _expand_rect(width, height, (_even(x), _even(y), _even(w), _even(h)), pad=pad)


def _delogo_capabilities() -> dict[str, bool]:
    """探测当前 ffmpeg 的 delogo 支持项（imageio 4.2.x 无 band）。"""
    global _delogo_caps_cache
    if _delogo_caps_cache is not None:
        return _delogo_caps_cache
    caps = {"band": False, "show": False}
    try:
        ffmpeg = resolve_ffmpeg_exe()
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "filter=delogo"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
        caps["band"] = bool(re.search(r"^\s+band\s+", text, re.MULTILINE))
        caps["show"] = bool(re.search(r"^\s+show\s+", text, re.MULTILINE))
    except OSError as exc:
        logger.debug("delogo capability probe failed: %s", exc)
    _delogo_caps_cache = caps
    logger.debug("delogo capabilities: %s", caps)
    return caps


def _delogo_filter(x: int, y: int, w: int, h: int, *, band: int) -> str:
    caps = _delogo_capabilities()
    opts = [f"x={x}", f"y={y}", f"w={w}", f"h={h}"]
    if caps.get("band"):
        opts.append(f"band={band}")
    if caps.get("show"):
        opts.append("show=0")
    return "delogo=" + ":".join(opts)


def _validate_rects(vw: int, vh: int, rects: list[tuple[int, int, int, int]]) -> None:
    for rx, ry, rw, rh in rects:
        if rx + rw > vw or ry + rh > vh:
            raise RuntimeError(
                f"水印区域 ({rx},{ry},{rw}x{rh}) 超出画面 {vw}x{vh}，请缩小或换预设"
            )


def _build_delogo_vf(
    rects: list[tuple[int, int, int, int]],
    profile: _WmProfile,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    scale_first: bool,
    defer_output: bool = False,
) -> str:
    band = _env_int("HONGGUO_WM_DELOGO_BAND", profile.delogo_band)
    parts: list[str] = []
    if scale_first and (out_w != src_w or out_h != src_h):
        parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    parts.extend(_delogo_filter(x, y, w, h, band=band) for x, y, w, h in rects)
    chain = ",".join(parts)
    if _use_tmix():
        if _env_int("HONGGUO_WM_DEFLICKER", 1):
            chain += f",{_hqdn3d_vf(for_delogo=True)}"
        chain += f",{_tmix_vf()}"
    if not defer_output:
        chain += f",{_fps_vf()}"
        if not scale_first:
            scale = _scale_vf(src_w, src_h)
            if scale:
                chain += f",{scale}"
    return chain


def _build_blur_cover_filter_complex(
    rects: list[tuple[int, int, int, int]],
    profile: _WmProfile,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    scale_first: bool,
    defer_output: bool = False,
) -> tuple[str, str]:
    """
    高斯模糊后不透明叠回（默认）；HONGGUO_WM_FEATHER>0 时边缘羽化。
    返回 (filter_complex, output_label)。
    """
    sigma = _gblur_sigma(profile)
    feather = _env_int("HONGGUO_WM_FEATHER", profile.feather_px)

    steps: list[str] = []
    if scale_first and (out_w != src_w or out_h != src_h):
        steps.append(f"[0:v]scale={out_w}:{out_h}:flags=lanczos[vb]")
        current = "[vb]"
        vw, vh = out_w, out_h
    else:
        current = "[0:v]"
        vw, vh = src_w, src_h

    for i, (x, y, w, h) in enumerate(rects):
        out = f"[vw{i}]"
        steps.append(f"{current}split=2[b{i}][s{i}]")
        if feather > 0:
            ex, ey, ew, eh = _expand_for_feather(vw, vh, x, y, w, h, feather)
            steps.append(
                f"[s{i}]crop={ew}:{eh}:{ex}:{ey},gblur=sigma={sigma}:steps=1,"
                f"format=yuva420p,{_feather_geq(feather)}[p{i}]"
            )
            ox, oy = ex, ey
        else:
            ex, ey, ew, eh = _expand_rect(vw, vh, (x, y, w, h), pad=4)
            steps.append(
                f"[s{i}]crop={ew}:{eh}:{ex}:{ey},gblur=sigma={sigma}:steps=1,"
                f"boxblur={max(8, int(sigma))}:{max(4, int(sigma // 2))}[p{i}]"
            )
            ox, oy = ex, ey
        steps.append(f"[b{i}][p{i}]overlay={ox}:{oy}{out}")
        current = out

    if defer_output:
        steps.append(f"{current}null[vout]")
    else:
        tail = _fps_vf()
        if not scale_first:
            scale = _scale_vf(src_w, src_h)
            if scale:
                tail += f",{scale}"
        steps.append(f"{current}{tail}[vout]")

    return ";".join(steps), "[vout]"


def _run_ffmpeg(
    src: Path,
    dest: Path,
    *,
    vf: str | None = None,
    filter_complex: str | None = None,
    map_video: str | None = None,
    apply_output_fps: bool = True,
) -> None:
    ffmpeg = resolve_ffmpeg_exe()
    enc_preset = os.getenv("HONGGUO_WM_ENCODE_PRESET", "medium").strip() or "medium"
    crf = os.getenv("HONGGUO_WM_CRF", "21").strip() or "21"
    threads = os.getenv("HONGGUO_FFMPEG_THREADS", "4").strip() or "4"
    fps = output_fps()
    x264 = _wm_x264_params()

    args = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        threads,
        "-i",
        str(src),
    ]
    if filter_complex:
        args.extend(["-filter_complex", filter_complex])
        args.extend(["-map", map_video or "[vout]", "-map", "0:a?"])
    else:
        args.extend(["-vf", vf or "null", "-map", "0:v", "-map", "0:a?"])

    args.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            enc_preset,
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            *(
                ["-r", str(fps)]
                if apply_output_fps
                else []
            ),
            "-x264-params",
            x264,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=2400,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"去水印失败: {tail}")


def _current_enhance() -> str:
    from video_enhance import normalize_enhance

    job = _wm_job_enhance.get()
    return normalize_enhance(job if job else None)


def _emit_final_output(
    video: Path,
    audio_src: Path,
    dest: Path,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> str:
    """OpenCV/FFmpeg 中间片 → 可选 Real-ESRGAN 超分 → 缩放/插帧 → 混音。"""
    from video_enhance import apply_sr_output, mux_video, sr_enabled

    fps = output_fps()
    if sr_enabled(_current_enhance()):
        return apply_sr_output(
            video,
            audio_src,
            dest,
            src_w=src_w,
            src_h=src_h,
            out_w=out_w,
            out_h=out_h,
            fps=fps,
        )

    vf_parts: list[str] = []
    if out_w != src_w or out_h != src_h:
        vf_parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    if fps > 0:
        vf_parts.append(f"fps={fps}")
    mux_video(video, audio_src, dest, vf=",".join(vf_parts) if vf_parts else None)
    return "lanczos"


def _finalize_inpaint_video(
    video: Path,
    audio_src: Path,
    dest: Path,
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
) -> str:
    """OpenCV 输出后：超分/缩放、帧率、音频、H.264。"""
    return _emit_final_output(
        video,
        audio_src,
        dest,
        src_w=src_w,
        src_h=src_h,
        out_w=out_w,
        out_h=out_h,
    )


def remove_watermark(
    src: Path,
    dest: Path,
    *,
    preset: str = "bottom-right",
    strength: str = "normal",
    method: str | None = None,
    x: int | None = None,
    y: int | None = None,
    w: int | None = None,
    h: int | None = None,
    output_scale: str | None = None,
    output_fps_choice: str | int | None = None,
    enhance: str | None = None,
) -> dict[str, int | str | list]:
    if not src.is_file():
        raise RuntimeError("源视频不存在")
    if src.stat().st_size < 10_000:
        raise RuntimeError("视频文件过小或已损坏")

    from video_enhance import normalize_enhance

    scale_tok = _wm_job_scale.set(normalize_output_scale(output_scale) if output_scale else "")
    fps_tok = _wm_job_fps.set(normalize_output_fps_choice(output_fps_choice))
    src_fps_tok = _wm_source_fps.set(probe_video_fps(src))
    enhance_tok = _wm_job_enhance.set(
        normalize_enhance(enhance) if enhance else normalize_enhance(None)
    )
    try:
        return _remove_watermark_impl(
            src,
            dest,
            preset=preset,
            strength=strength,
            method=method,
            x=x,
            y=y,
            w=w,
            h=h,
        )
    finally:
        _wm_job_scale.reset(scale_tok)
        _wm_job_fps.reset(fps_tok)
        _wm_source_fps.reset(src_fps_tok)
        _wm_job_enhance.reset(enhance_tok)


def _remove_watermark_impl(
    src: Path,
    dest: Path,
    *,
    preset: str,
    strength: str,
    method: str | None,
    x: int | None,
    y: int | None,
    w: int | None,
    h: int | None,
) -> dict[str, int | str | list]:
    vw, vh = probe_video_size(src)
    if x is not None and y is not None and w is not None and h is not None:
        rects = [rect_from_custom(vw, vh, x, y, w, h)]
        mode = "custom"
    else:
        rects = rects_for_preset(vw, vh, preset, strength=strength, src=src)
        mode = preset

    prof = _profile(strength)
    strength_used = wm_strength(strength)
    out_spec = _output_canvas_spec(vw, vh)
    ow, oh = out_spec.width, out_spec.height
    rects_src = list(rects)
    scale_first = _scale_before_wm() and (ow != vw or oh != vh)
    rects_out = _scale_rects(rects_src, vw, vh, ow, oh) if scale_first else rects_src
    proc_w, proc_h = (ow, oh) if scale_first else (vw, vh)
    _validate_rects(proc_w, proc_h, rects_out)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    use_sr = _current_enhance() == "sr"
    defer_output = use_sr
    output_backend = "lanczos"

    method = wm_method(method)
    if method == "inpaint":
        if not opencv_inpaint_available():
            raise RuntimeError(
                "智能修复需要 OpenCV。请在 server 目录执行："
                "py -3.12 -m pip install opencv-python-headless"
            )
        from watermark_inpaint import inpaint_video

        work = dest.with_suffix(".inpaint.mp4")
        radius = _env_int(
            "HONGGUO_WM_INPAINT_RADIUS",
            10 if strength_used == "strong" else 8,
        )
        dilate = _env_int(
            "HONGGUO_WM_INPAINT_DILATE",
            {"tight": 4, "normal": 6, "strong": 10}.get(strength_used, 6),
        )
        _validate_rects(vw, vh, rects_src)
        inpaint_video(src, work, rects_src, radius=radius, dilate_px=dilate, strength=strength_used)
        output_backend = _finalize_inpaint_video(
            work,
            src,
            dest,
            src_w=vw,
            src_h=vh,
            out_w=ow,
            out_h=oh,
        )
        work.unlink(missing_ok=True)
        method_used = "inpaint"
    elif method == "blur_cover":
        fc, vlabel = _build_blur_cover_filter_complex(
            rects_out,
            prof,
            src_w=vw,
            src_h=vh,
            out_w=ow,
            out_h=oh,
            scale_first=scale_first and not defer_output,
            defer_output=defer_output,
        )
        if defer_output:
            work = dest.with_suffix(".wmwork.mp4")
            _run_ffmpeg(
                src,
                work,
                filter_complex=fc,
                map_video=vlabel,
                apply_output_fps=False,
            )
            output_backend = _emit_final_output(
                work, src, dest, src_w=vw, src_h=vh, out_w=ow, out_h=oh
            )
            work.unlink(missing_ok=True)
        else:
            _run_ffmpeg(src, dest, filter_complex=fc, map_video=vlabel)
        method_used = "blur_cover"
    else:
        vf = _build_delogo_vf(
            rects_out,
            prof,
            src_w=vw,
            src_h=vh,
            out_w=ow,
            out_h=oh,
            scale_first=scale_first and not defer_output,
            defer_output=defer_output,
        )
        if defer_output:
            work = dest.with_suffix(".wmwork.mp4")
            _run_ffmpeg(src, work, vf=vf, apply_output_fps=False)
            output_backend = _emit_final_output(
                work, src, dest, src_w=vw, src_h=vh, out_w=ow, out_h=oh
            )
            work.unlink(missing_ok=True)
        else:
            _run_ffmpeg(src, dest, vf=vf)
        method_used = "delogo"

    if not dest.is_file() or dest.stat().st_size < 10_000:
        raise RuntimeError("输出视频过小，处理可能失败")

    tl_segments: list[tuple[float, float]] = []
    if _tl_refine_enabled(mode) and method_used == "blur_cover":
        tl_rect = _find_top_left_rect(rects_out, frame_h=proc_h)
        if tl_rect:
            tl_segments = detect_top_left_watermark_segments(
                src, vw, vh, duration=probe_duration(src)
            )
            if tl_segments:
                _refine_top_left_timed(
                    dest,
                    tl_rect,
                    tl_segments,
                    prof,
                    vw=proc_w,
                    vh=proc_h,
                )

    regions = [{"x": rx, "y": ry, "w": rw, "h": rh} for rx, ry, rw, rh in rects_out]
    logger.info(
        "去水印完成 %s → %s mode=%s method=%s regions=%d",
        src.name,
        dest.name,
        mode,
        method_used,
        len(rects_out),
    )
    return {
        "mode": mode,
        "method": method_used,
        "strength": strength_used,
        "video_width": vw,
        "video_height": vh,
        "output_width": out_spec.width,
        "output_height": out_spec.height,
        "output_fps": output_fps(),
        "output_label": out_spec.label,
        "enhance": _current_enhance(),
        "enhance_backend": output_backend,
        "regions": regions,
        "ocr": src is not None and mode != "custom",
        "tl_refine_segments": [
            {"start": s, "end": e} for s, e in tl_segments
        ],
        "x": rects_out[0][0],
        "y": rects_out[0][1],
        "w": rects_out[0][2],
        "h": rects_out[0][3],
    }
