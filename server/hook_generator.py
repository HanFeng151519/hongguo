import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from external_material import fetch_external_body_source
from ai_edit_planner import (
    BodySegmentPlan,
    HookEditPlan,
    default_plan,
    plan_hook_edit,
    plan_to_dict,
)
from fq_koc_material import (
    fetch_fq_koc_episode,
    find_local_material,
    is_configured as fq_koc_configured,
    local_material_duration,
    raise_material_download_error,
)
from video_drm import audio_is_audible, video_decodes
from video_originality import (
    authentic_preservation_enabled,
    body_burn_subtitles,
    body_playback_speed,
    build_body_video_filters,
    build_subtitle_overlay_filter_complex,
    ffmpeg_metadata_strip_args,
    originality_enabled,
    originality_light_visual,
    originality_outro_enabled,
    pick_commentary_lines,
    render_subtitle_overlay_png,
    trim_jitter_seconds,
)
from edge_tts_narration import (
    edge_tts_available,
    fixed_opening_text,
    opening_card_duration_for_text,
    pick_voice_for_episode,
    probe_media_duration,
    resolve_voice,
    tts_enabled,
    tts_on_body_enabled,
)

TTS_CACHE_DIR = Path(__file__).resolve().parent.parent / "public" / "tts_cache"
from hook_duration_budget import (
    budget_summary,
    hook_budget_enabled,
    hook_duration_range_text,
)
from hook_timeline import (
    fixed_opening_line,
    fixed_outro_line,
    golden_open_sec,
    outro_cta_sec,
    pro_60_template_enabled,
    timeline_summary,
)
from platform_compliance import (
    commentary_footer_hint,
    compliance_brand_name,
    douyin_safe_enabled,
    outro_card_lines,
    splash_subtitle_text,
)
from meme_edit import meme_edit_enabled, meme_on_body_enabled
from multi_clip import (
    clip_crossfade_sec,
    clip_summary_for_log,
    clip_tail_pad_sec,
    multi_clip_enabled,
)
from watermark import burn_corner_watermark, watermark_enabled, watermark_text
from moviepy_editor import (
    image_to_video as mpy_image_to_video,
    merge_video_files as mpy_merge_video_files,
    moviepy_available,
    normalize_video_file as mpy_normalize_video_file,
    process_body_clip as mpy_process_body_clip,
    video_backend,
)

logger = logging.getLogger(__name__)

OPENING_SECONDS = 3
OUTRO_SECONDS = 4
KEYWORD_SPLASH_SECONDS = 1.0
COMMENTARY_CARD_SECONDS = 1.2
OUTRO_SEARCH_SECONDS = 2.0
HONGGUO_BRAND_NAME = "红果短剧"
# 片头渐变四角色（取自品牌 logo：橙红 → 蜜桃 → 薄荷青）
_SPLASH_GRAD_TL = (255, 209, 148)
_SPLASH_GRAD_TR = (128, 216, 200)
_SPLASH_GRAD_BL = (242, 101, 34)
_SPLASH_GRAD_BR = (255, 140, 66)
_SPLASH_LOGO_MARGIN_X = 48
_SPLASH_LOGO_MARGIN_Y = 40
_SPLASH_LOGO_ICON_H = 108
# 片头标题卡字号：关键词起始字号、最小字号、每「号」像素差（副标题比关键词小两号）
SPLASH_TITLE_FONT_START = 160
SPLASH_TITLE_FONT_MIN = 72
SPLASH_FONT_GRADE_PX = 28
SPLASH_SUBTITLE_FONT_MIN = 64
SPLASH_TITLE_FONT_DEFAULT = 120
SPLASH_SUBTITLE_FONT_DEFAULT = 70
SPLASH_BADGE_FONT_DEFAULT = 56
SPLASH_BADGE_FONT_MIN = 36
MAX_BODY_SECONDS = 3600  # 单集正片最长 1 小时，防止异常时长
# 推广成片：横屏 16:9 = 1920×1080（与推广中心正片同比例）
WORK_WIDTH = 1920
WORK_HEIGHT = 1080
ASPECT_LABEL = "16:9"
PANEL_WIDTH = 1920
MAX_EPISODES = 6
MIN_OUTPUT_BYTES = 400_000
AUDIO_RATE = 44100
OUTPUT_FPS = 30
OUTPUT_CRF = 20
ENCODE_PRESET = "fast"
BODY_ENCODE_PRESET = "veryfast"  # 正片去重重编码，加快多集成片
def _use_moviepy() -> bool:
    return video_backend() == "moviepy" and moviepy_available()


DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://reading.snssdk.com/",
    "Accept": "*/*",
}


def _resolve_ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate

    raise RuntimeError(
        "未找到 ffmpeg。请执行: brew install ffmpeg "
        "或 pip install imageio-ffmpeg"
    )


FFMPEG = _resolve_ffmpeg()


def _run_ffmpeg(args: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(
        [FFMPEG, "-y", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"视频处理失败: {tail}")


def _resolve_ffprobe() -> Optional[str]:
    found = shutil.which("ffprobe")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"):
        if Path(candidate).is_file():
            return candidate
    return None


def _parse_ffmpeg_duration(stderr: str) -> float:
    for line in (stderr or "").splitlines():
        if "Duration:" in line:
            part = line.split("Duration:", 1)[1].split(",")[0].strip()
            h, m, s = part.split(":")
            return float(h) * 3600 + float(m) * 60 + float(s)
    return 0.0


def _probe_duration(path: Path) -> float:
    """读取容器时长（不整段解码，避免大文件 probe 超时）。"""
    if not path.is_file():
        return 0.0
    ffprobe = _resolve_ffprobe()
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
                timeout=15,
            )
            if proc.returncode == 0:
                raw = (proc.stdout or "").strip().splitlines()
                if raw:
                    return max(0.0, float(raw[0]))
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            logger.debug("ffprobe 读取时长失败 %s: %s", path.name, exc)

    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg 读取时长超时: %s", path)
        return 0.0
    return _parse_ffmpeg_duration(proc.stderr or "")


def _extract_leading_clip(src: Path, dest: Path, seconds: float) -> None:
    """截取片头若干秒（用于黄金口播入场）。"""
    dest.unlink(missing_ok=True)
    dur = max(0.5, float(seconds))
    _run_ffmpeg(
        [
            "-hide_banner",
            "-i",
            str(src),
            "-t",
            f"{dur:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout=120,
    )


def _extend_video_to_duration(src: Path, dest: Path, target_sec: float) -> None:
    """正片短于预算时末帧定格补齐（保证整条钩子接近 60s）。"""
    target = max(0.5, float(target_sec))
    dur = _probe_duration(src) or 0.0
    if dur >= target - 0.2:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return
    pad = target - dur
    dest.unlink(missing_ok=True)
    _run_ffmpeg(
        [
            "-hide_banner",
            "-i",
            str(src),
            "-vf",
            f"scale={WORK_WIDTH}:{WORK_HEIGHT},tpad=stop_mode=clone:stop_duration={pad:.3f}",
            "-af",
            f"apad=pad_dur={pad:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            ENCODE_PRESET,
            "-crf",
            str(OUTPUT_CRF),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout=300,
    )


def _trim_leading_clip(src: Path, dest: Path, skip_sec: float) -> None:
    """去掉片头若干秒（黄金口播与正片不重复）。"""
    skip = max(0.0, float(skip_sec))
    if skip < 0.05:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return
    dest.unlink(missing_ok=True)
    _run_ffmpeg(
        [
            "-hide_banner",
            "-ss",
            f"{skip:.3f}",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout=180,
    )


def _has_audio(path: Path) -> bool:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return "Audio:" in (proc.stderr or "")


def _video_bitrate_bps(path: Path) -> int:
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    line = (proc.stdout or "").strip().splitlines()
    if line and line[0].isdigit():
        return int(line[0])
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    line = (proc.stdout or "").strip().splitlines()
    if line and line[0].isdigit():
        return int(line[0])
    size = path.stat().st_size if path.is_file() else 0
    dur = _probe_duration(path) or 1.0
    return int(size * 8 / dur) if dur > 0 else 0


def _probe_video_size(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return 0, 0


def _video_codec(path: Path) -> str:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in (proc.stderr or "").splitlines():
        if "Video:" not in line:
            continue
        match = re.search(r"Video:\s*(\w+)", line)
        if match:
            return match.group(1).lower()
    return ""


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    text = text.strip()
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/STHeiti Medium.ttc",
            ]
        )
    candidates.extend(
        [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _compose_panel_canvas(
    cover_path: Optional[Path],
    *,
    bg_color: tuple[int, int, int] = (12, 8, 10),
) -> Image.Image:
    """1920×1080 横屏画布，全宽放海报/画面。"""
    canvas = Image.new("RGB", (WORK_WIDTH, WORK_HEIGHT), (0, 0, 0))

    if cover_path and cover_path.is_file():
        poster = Image.open(cover_path).convert("RGB")
        panel = ImageOps.fit(
            poster, (PANEL_WIDTH, WORK_HEIGHT), method=Image.Resampling.LANCZOS
        )
    else:
        panel = Image.new("RGB", (PANEL_WIDTH, WORK_HEIGHT), bg_color)

    canvas.paste(panel, (0, 0))
    return canvas


async def _download_cover(client: httpx.AsyncClient, url: str, dest: Path) -> bool:
    if not url.strip():
        return False
    headers = {
        **DOWNLOAD_HEADERS,
        "Referer": "https://www.novelquickapp.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        resp = await client.get(
            url.strip(),
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        with Image.open(dest) as im:
            im.convert("RGB").save(dest, format="JPEG", quality=92)
        return dest.stat().st_size > 1000
    except Exception as exc:
        logger.warning("海报下载失败: %s", exc)
        return False


def render_opening_card(path: Path, cover_path: Optional[Path], opening: str) -> None:
    img = _compose_panel_canvas(cover_path, bg_color=(24, 16, 20))
    overlay = Image.new("RGBA", (WORK_WIDTH, WORK_HEIGHT), (0, 0, 0, 90))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    font = _load_font(40, bold=True)
    margin_x = (WORK_WIDTH - PANEL_WIDTH) // 2 + 20
    max_w = PANEL_WIDTH - 40
    lines: list[str] = []
    for para in opening.replace("\r", "").split("\n"):
        para = para.strip()
        if not para:
            continue
        lines.extend(_wrap_text(draw, para, font, max_w))
    lines = lines[:4]
    y = WORK_HEIGHT - 30 - len(lines) * 52
    for line in lines:
        draw.text(
            (margin_x, y),
            line,
            fill=(255, 255, 255),
            font=font,
            stroke_width=2,
            stroke_fill=(0, 0, 0),
        )
        y += 52
    img.save(path)


def _hongguo_brand_asset_path() -> Path:
    return Path(__file__).resolve().parent.parent / "public" / "assets" / "hongguo_brand.png"


def _hongguo_splash_gradient(width: int, height: int) -> Image.Image:
    """品牌色对角混搭渐变（左下橙红、右上薄荷青）。"""
    tiny = Image.new("RGB", (2, 2))
    tiny.putpixel((0, 0), _SPLASH_GRAD_TL)
    tiny.putpixel((1, 0), _SPLASH_GRAD_TR)
    tiny.putpixel((0, 1), _SPLASH_GRAD_BL)
    tiny.putpixel((1, 1), _SPLASH_GRAD_BR)
    return tiny.resize((width, height), Image.Resampling.LANCZOS)


def _strip_logo_corner_padding(icon: Image.Image) -> Image.Image:
    """方形 logo：四角白底转透明，再收紧到实际内容边界。"""
    icon = icon.convert("RGBA")
    w, h = icon.size
    px = icon.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 8:
                continue
            if r >= 238 and g >= 238 and b >= 238 and max(r, g, b) - min(r, g, b) < 22:
                px[x, y] = (0, 0, 0, 0)
    bbox = icon.getbbox()
    if bbox:
        icon = icon.crop(bbox)
    return icon


def _hongguo_brand_icon_rgba() -> Optional[Image.Image]:
    """加载品牌圆角图标（去四角留白，透明底）。"""
    path = _hongguo_brand_asset_path()
    if not path.is_file():
        return None
    with Image.open(path) as im:
        return _strip_logo_corner_padding(im)


def _draw_hongguo_brand_corner(img: Image.Image) -> Image.Image:
    """左上角：品牌圆角图标 + 白色「红果短剧」。"""
    icon = _hongguo_brand_icon_rgba()
    if icon is None:
        return img

    target_h = _SPLASH_LOGO_ICON_H
    scale = target_h / icon.size[1]
    target_w = max(1, int(icon.size[0] * scale))
    icon = icon.resize((target_w, target_h), Image.Resampling.LANCZOS)

    base = img.convert("RGBA")
    mx, my = _SPLASH_LOGO_MARGIN_X, _SPLASH_LOGO_MARGIN_Y
    base.paste(icon, (mx, my), icon)

    draw = ImageDraw.Draw(base)
    brand_font = _load_font(44, bold=True)
    brand_label = compliance_brand_name()
    bbox = draw.textbbox((0, 0), brand_label, font=brand_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = mx + target_w + 18 - bbox[0]
    ty = my + (target_h - th) // 2 - bbox[1]
    draw.text(
        (tx, ty),
        compliance_brand_name(),
        fill=(255, 255, 255, 255),
        font=brand_font,
        stroke_width=2,
        stroke_fill=(30, 30, 30),
    )
    return base.convert("RGB")


def _format_splash_title(keyword: str) -> str:
    fallback = os.getenv("HONGGUO_SPLASH_TITLE_FALLBACK", "短剧").strip() or "短剧"
    lb = os.getenv("HONGGUO_SPLASH_TITLE_LBRACKET", "《").strip() or "《"
    rb = os.getenv("HONGGUO_SPLASH_TITLE_RBRACKET", "》").strip() or "》"
    text = keyword.strip() or fallback
    if text.startswith(lb) and text.endswith(rb):
        return text
    return f"{lb}{text}{rb}"


def _splash_subtitle_font_size(title_size: int) -> int:
    """副标题比关键词小两号，且不低于 SPLASH_SUBTITLE_FONT_MIN。"""
    return max(
        SPLASH_SUBTITLE_FONT_MIN,
        title_size - 2 * SPLASH_FONT_GRADE_PX,
    )


def _clamp_splash_font(px: int, *, min_px: int, max_px: int) -> int:
    return max(min_px, min(max_px, int(px)))


def splash_font_sizes_from_env() -> tuple[Optional[int], Optional[int]]:
    """.env：HONGGUO_SPLASH_TITLE_FONT / HONGGUO_SPLASH_SUBTITLE_FONT"""
    title: Optional[int] = None
    subtitle: Optional[int] = None
    raw_t = os.getenv("HONGGUO_SPLASH_TITLE_FONT", "").strip()
    raw_s = os.getenv("HONGGUO_SPLASH_SUBTITLE_FONT", "").strip()
    if raw_t:
        try:
            title = _clamp_splash_font(int(raw_t), min_px=48, max_px=220)
        except ValueError:
            pass
    if raw_s:
        try:
            subtitle = _clamp_splash_font(int(raw_s), min_px=32, max_px=180)
        except ValueError:
            pass
    return title, subtitle


def _auto_fit_splash_fonts(draw: ImageDraw.ImageDraw, title: str) -> tuple[int, int]:
    max_w = WORK_WIDTH - 100
    size = SPLASH_TITLE_FONT_START
    while size > SPLASH_TITLE_FONT_MIN:
        title_font = _load_font(size, bold=True)
        sub_size = _splash_subtitle_font_size(size)
        sub_font = _load_font(sub_size, bold=False)
        tbox_try = draw.textbbox((0, 0), title, font=title_font)
        sbox_try = draw.textbbox((0, 0), splash_subtitle_text(), font=sub_font)
        if (tbox_try[2] - tbox_try[0] <= max_w) and (sbox_try[2] - sbox_try[0] <= max_w):
            return size, sub_size
        size -= 4
    sub_size = _splash_subtitle_font_size(size)
    return size, sub_size


def render_keyword_splash_card(
    path: Path,
    keyword: str,
    *,
    title_font_px: Optional[int] = None,
    subtitle_font_px: Optional[int] = None,
    badge_text: str = "",
) -> None:
    """1 秒片头：品牌渐变底 + 左上 logo + 白字《关键词》+ 副标题 + 可选角标。"""
    title = _format_splash_title(keyword)
    badge = (badge_text or "").strip()[:24]
    img = _hongguo_splash_gradient(WORK_WIDTH, WORK_HEIGHT)
    img = _draw_hongguo_brand_corner(img)
    draw = ImageDraw.Draw(img)

    if title_font_px is None and subtitle_font_px is None:
        env_t, env_s = splash_font_sizes_from_env()
        title_font_px = env_t
        subtitle_font_px = env_s

    if title_font_px is not None:
        size = _clamp_splash_font(title_font_px, min_px=48, max_px=220)
        if subtitle_font_px is not None:
            sub_size = _clamp_splash_font(subtitle_font_px, min_px=32, max_px=180)
        else:
            sub_size = _splash_subtitle_font_size(size)
    else:
        size, sub_size = _auto_fit_splash_fonts(draw, title)

    title_font = _load_font(size, bold=True)
    sub_font = _load_font(sub_size, bold=False)
    badge_size = _clamp_splash_font(
        max(SPLASH_BADGE_FONT_MIN, int(sub_size * 0.85)),
        min_px=SPLASH_BADGE_FONT_MIN,
        max_px=120,
    )
    badge_font = _load_font(badge_size, bold=True) if badge else None

    tbox = draw.textbbox((0, 0), title, font=title_font)
    tw, th = tbox[2] - tbox[0], tbox[3] - tbox[1]
    sub_line = splash_subtitle_text()
    sbox = draw.textbbox((0, 0), sub_line, font=sub_font)
    sw, sh = sbox[2] - sbox[0], sbox[3] - sbox[1]
    bbox = (
        draw.textbbox((0, 0), badge, font=badge_font)
        if badge and badge_font
        else (0, 0, 0, 0)
    )
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]

    title_sub_gap = max(36, int(size * 0.22))
    badge_gap = max(28, int(sub_size * 0.35)) if badge else 0
    block_h = th + title_sub_gap + sh + (badge_gap + bh if badge else 0)
    y0 = (WORK_HEIGHT - block_h) // 2

    tx = (WORK_WIDTH - tw) // 2 - tbox[0]
    draw.text(
        (tx, y0 - tbox[1]),
        title,
        fill=(255, 255, 255),
        font=title_font,
        stroke_width=3,
        stroke_fill=(30, 30, 30),
    )

    sx = (WORK_WIDTH - sw) // 2 - sbox[0]
    sy = y0 + th + title_sub_gap - sbox[1]
    draw.text(
        (sx, sy),
        sub_line,
        fill=(255, 255, 255),
        font=sub_font,
        stroke_width=2,
        stroke_fill=(40, 40, 40),
    )

    if badge and badge_font:
        bx = (WORK_WIDTH - bw) // 2 - bbox[0]
        by = sy + sh + badge_gap - bbox[1]
        draw.text(
            (bx, by),
            badge,
            fill=(255, 255, 255),
            font=badge_font,
            stroke_width=2,
            stroke_fill=(40, 40, 40),
        )

    img.save(path)


def render_commentary_card(
    path: Path,
    text: str,
    *,
    drama_title: str = "",
    minimal: bool = False,
) -> None:
    """解说卡：原创过渡画面，降低判重风险。"""
    short = (drama_title or "短剧").strip()[:12]
    body = (text or "").strip()
    if minimal:
        lines = [body] if body else [short]
    else:
        body = body or f"《{short}》高能片段"
        lines = [ln.strip() for ln in re.split(r"[\n；;]+", body) if ln.strip()][:3]
        if not lines:
            lines = [f"《{short}》这段太顶了"]

    img = Image.new("RGB", (WORK_WIDTH, WORK_HEIGHT), (18, 12, 16))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, WORK_WIDTH, 8), fill=(255, 77, 79))

    title_font = _load_font(56, bold=True)
    sub_font = _load_font(36, bold=False)
    max_w = WORK_WIDTH - 160
    y = (WORK_HEIGHT - len(lines) * 72) // 2
    for i, line in enumerate(lines):
        font = title_font if i == 0 else sub_font
        wrapped = _wrap_text(draw, line, font, max_w)[:2]
        for wl in wrapped:
            box = draw.textbbox((0, 0), wl, font=font)
            tw = box[2] - box[0]
            draw.text(
                ((WORK_WIDTH - tw) // 2 - box[0], y - box[1]),
                wl,
                fill=(255, 255, 255),
                font=font,
            )
            y += 72

    if not minimal:
        hint = commentary_footer_hint(drama_title)
        hint_font = _load_font(32, bold=False)
        hbox = draw.textbbox((0, 0), hint, font=hint_font)
        draw.text(
            (
                (WORK_WIDTH - (hbox[2] - hbox[0])) // 2 - hbox[0],
                WORK_HEIGHT - 90 - hbox[1],
            ),
            hint,
            fill=(255, 180, 120),
            font=hint_font,
        )
    img.save(path)


def render_outro_card(
    path: Path,
    keyword: str,
    *,
    cta_line: str = "",
) -> None:
    """片尾引导卡；cta_line 非空时为专业尾帧大号话术。"""
    img = Image.new("RGB", (WORK_WIDTH, WORK_HEIGHT), (8, 8, 12))
    draw = ImageDraw.Draw(img)
    full = (cta_line or "").strip()
    if full:
        font = _load_font(64, bold=True)
        box = draw.textbbox((0, 0), full, font=font, stroke_width=3)
        tw = box[2] - box[0]
        draw.text(
            (
                (WORK_WIDTH - tw) // 2 - box[0],
                WORK_HEIGHT // 2 - box[1],
            ),
            full,
            fill=(255, 255, 255),
            font=font,
            stroke_width=3,
            stroke_fill=(0, 0, 0),
        )
        img.save(path)
        return

    kw = (keyword or "短剧").strip()[:16]
    line1, line2 = outro_card_lines(kw)
    f1 = _load_font(48, bold=True)
    f2 = _load_font(56, bold=True)
    for text, font, y_off, color in (
        (line1, f1, -60, (220, 220, 220)),
        (line2, f2, 30, (255, 77, 79)),
    ):
        box = draw.textbbox((0, 0), text, font=font)
        tw = box[2] - box[0]
        draw.text(
            ((WORK_WIDTH - tw) // 2 - box[0], WORK_HEIGHT // 2 + y_off - box[1]),
            text,
            fill=color,
            font=font,
        )
    img.save(path)


def _body_effects_for_clip(
    commentary_lines: Optional[list[str]],
    meme_captions: Optional[list],
    meme_beats: Optional[list],
) -> tuple[list[str], list, list]:
    """原味模式：正片不叠 meme/解说条/TTS，仅保留裁剪与轻度像素去重。"""
    caps = list(meme_captions or [])
    beats = list(meme_beats or [])
    lines = list(commentary_lines or [])
    if not meme_on_body_enabled():
        caps, beats = [], []
    if not body_burn_subtitles() and not tts_on_body_enabled():
        lines = []
    elif not body_burn_subtitles():
        lines = []
    return lines, caps, beats


def _video_output_fit_filter() -> str:
    """异比例素材缩放并居中 pad 到 1920×1080 横屏。"""
    w, h = WORK_WIDTH, WORK_HEIGHT
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={OUTPUT_FPS},format=yuv420p"
    )


def _video_scale_pad_filter() -> str:
    return _video_output_fit_filter()


def _atempo_chain(speed: float) -> str:
    """ffmpeg atempo 单次仅支持 0.5–2.0，高倍速时串联。"""
    if abs(speed - 1.0) < 0.01:
        return ""
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0 + 0.01:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 0.01:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.01:
        filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def _body_vf_with_speed(
    speed: float,
    *,
    seed_str: str = "",
) -> str:
    base = _video_scale_pad_filter()
    if originality_enabled():
        return build_body_video_filters(
            base_fit_filter=base,
            speed=speed,
            seed_str=seed_str or "default",
            width=WORK_WIDTH,
            height=WORK_HEIGHT,
            fps=OUTPUT_FPS,
        )
    parts = [base]
    if abs(speed - 1.0) >= 0.01:
        parts.append(f"setpts=PTS/{speed}")
    return ",".join(parts)


def _normalize_segment(src: Path, dest: Path, *, seconds: float = 0) -> None:
    """统一帧率/分辨率/音轨，避免拼接后卡顿。"""
    if _use_moviepy():
        try:
            mpy_normalize_video_file(src, dest, seconds=seconds)
            return
        except Exception as exc:
            logger.warning("MoviePy 规范化失败，回退 FFmpeg: %s", exc)
    dest.unlink(missing_ok=True)
    dur_hint = seconds or _probe_duration(src) or 120.0
    _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-vf",
            _video_scale_pad_filter(),
            "-c:v",
            "libx264",
            "-preset",
            ENCODE_PRESET,
            "-crf",
            str(OUTPUT_CRF),
            "-g",
            str(OUTPUT_FPS),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(AUDIO_RATE),
            "-ac",
            "2",
            "-af",
            "aresample=async=1:first_pts=0",
            *ffmpeg_metadata_strip_args(),
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout=_ffmpeg_timeout(dur_hint, base=300),
    )


def _encode_parallel_workers() -> int:
    try:
        return max(1, min(4, int(os.getenv("HONGGUO_ENCODE_PARALLEL", "2"))))
    except ValueError:
        return 2


def _clip_with_plan(
    raw: Path,
    dest: Path,
    label: str,
    fallback_seconds: float,
    segment_plan: Optional[BodySegmentPlan],
    *,
    prefer_stream_copy: bool = False,
    commentary_lines: Optional[list[str]] = None,
    originality_seed: str = "",
    work_dir: Optional[Path] = None,
) -> None:
    meme_caps = segment_plan.meme_captions if segment_plan else []
    meme_beats = segment_plan.meme_beats if segment_plan else []
    body_lines, meme_caps, meme_beats = _body_effects_for_clip(
        commentary_lines, meme_caps, meme_beats
    )
    clips = segment_plan.resolved_clips() if segment_plan else []
    use_multi = multi_clip_enabled() and len(clips) > 1

    if not use_multi:
        trim = segment_plan.trim_start_sec if segment_plan else 0.0
        if originality_enabled():
            trim = max(
                0.0,
                trim + trim_jitter_seconds(f"{originality_seed}:{label}:{trim:.1f}"),
            )
        max_d = segment_plan.duration_sec if segment_plan else None
        _process_body_clip(
            raw,
            dest,
            label,
            fallback_seconds,
            trim_start_sec=trim,
            max_duration_sec=max_d,
            prefer_stream_copy=prefer_stream_copy,
            commentary_lines=body_lines,
            originality_seed=originality_seed,
            work_dir=work_dir,
            meme_captions=meme_caps,
            meme_beats=meme_beats,
        )
        return

    work = work_dir or dest.parent
    parts: list[Path] = []
    try:
        def _encode_fragment(i: int, frag) -> tuple[int, Path]:
            part = work / f"{dest.stem}_f{i:02d}.mp4"
            trim = frag.trim_start_sec
            if originality_enabled():
                trim = max(
                    0.0,
                    trim
                    + trim_jitter_seconds(
                        f"{originality_seed}:{label}:f{i}:{trim:.1f}"
                    )
                    * 0.35,
                )
            _process_body_clip(
                raw,
                part,
                f"{label}·段{i + 1}",
                fallback_seconds,
                trim_start_sec=trim,
                max_duration_sec=frag.duration_sec,
                prefer_stream_copy=False,
                commentary_lines=[],
                originality_seed=f"{originality_seed}:f{i}",
                work_dir=work,
                meme_captions=[],
                meme_beats=[],
                clip_tail_pad=clip_tail_pad_sec(),
                soft_audio_fade_out=True,
                soft_audio_fade_in=(i == 0),
            )
            return i, part

        workers = _encode_parallel_workers()
        if workers > 1 and len(clips) > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            indexed: list[tuple[int, Path]] = []
            with ThreadPoolExecutor(max_workers=min(workers, len(clips))) as pool:
                futures = [
                    pool.submit(_encode_fragment, i, frag)
                    for i, frag in enumerate(clips)
                ]
                for fut in as_completed(futures):
                    indexed.append(fut.result())
            parts = [p for _, p in sorted(indexed, key=lambda x: x[0])]
            logger.info(
                "%s 并行编码 %d 段（workers=%d）",
                label,
                len(parts),
                min(workers, len(clips)),
            )
        else:
            for i, frag in enumerate(clips):
                _, part = _encode_fragment(i, frag)
                parts.append(part)
        if _use_moviepy():
            try:
                mpy_merge_video_files(
                    parts, dest, crossfade_sec=clip_crossfade_sec()
                )
            except Exception as exc:
                logger.warning("%s 快切交叉淡化失败，直拼: %s", label, exc)
                _merge_segments(parts, dest, body_seconds=0.0, intro_seconds=0.0)
        else:
            _merge_segments(parts, dest, body_seconds=0.0, intro_seconds=0.0)
        logger.info(
            "%s 本集高能快切 %d 段 → %.1fs [%s]",
            label,
            len(parts),
            _probe_duration(dest) or 0.0,
            clip_summary_for_log(clips),
        )
    finally:
        for p in parts:
            p.unlink(missing_ok=True)


def _ensure_output_aspect(segment: Path, label: str = "") -> None:
    """横屏/异比例素材统一为 1920×1080（16:9）。"""
    w, h = _probe_video_size(segment)
    if w == WORK_WIDTH and h == WORK_HEIGHT:
        return
    if w <= 0 or h <= 0:
        logger.warning("%s 无法读取分辨率，跳过画幅校正", label or segment.name)
        return
    tmp = segment.parent / f"{segment.stem}_169.mp4"
    dur = _probe_duration(segment) or 60.0
    _normalize_segment(segment, tmp, seconds=dur)
    segment.unlink(missing_ok=True)
    tmp.rename(segment)
    logger.info(
        "%s %dx%d → %dx%d (%s)",
        label or segment.name,
        w,
        h,
        WORK_WIDTH,
        WORK_HEIGHT,
        ASPECT_LABEL,
    )


def _ensure_segment_h264(segment: Path, label: str) -> None:
    """HEVC 正片转 H.264，避免与片头 H.264 无损拼接后画面黑屏。"""
    codec = _video_codec(segment)
    if codec not in ("hevc", "h265"):
        return
    tmp = segment.parent / f"{segment.stem}_h264.mp4"
    if not _finalize_for_browser(segment, tmp):
        raise RuntimeError(
            f"{label} 为 HEVC，转 H.264 失败。请确认 ffmpeg 可用，"
            "或重新从推广中心下载后覆盖本地 MP4。"
        )
    segment.unlink(missing_ok=True)
    tmp.rename(segment)
    logger.info("%s 已转为 H.264 供拼接", label)


def _ffmpeg_timeout(seconds: float, *, base: int = 300) -> int:
    return int(max(base, min(MAX_BODY_SECONDS, seconds) * 2))


def _process_body_clip(
    raw: Path,
    dest: Path,
    label: str,
    seconds: float,
    *,
    trim_start_sec: float = 0.0,
    max_duration_sec: Optional[float] = None,
    prefer_stream_copy: bool = False,
    commentary_lines: Optional[list[str]] = None,
    originality_seed: str = "",
    work_dir: Optional[Path] = None,
    meme_captions: Optional[list] = None,
    meme_beats: Optional[list] = None,
    clip_tail_pad: Optional[float] = None,
    soft_audio_fade_out: bool = False,
    soft_audio_fade_in: bool = False,
) -> None:
    """裁剪正片；开启去重增强时强制重编码并烧录解说字幕。"""
    trim_start = max(0.0, float(trim_start_sec or 0))
    speed = body_playback_speed()
    need_originality = originality_enabled()
    logger.info(
        "%s 正片倍速配置: %.3gx（HONGGUO_BODY_PLAYBACK_SPEED=%s, AUTHENTIC=%s）",
        label,
        speed,
        os.getenv("HONGGUO_BODY_PLAYBACK_SPEED", "(未设)"),
        os.getenv("HONGGUO_AUTHENTIC", "(未设)"),
    )
    body_lines, meme_captions, meme_beats = _body_effects_for_clip(
        commentary_lines, meme_captions, meme_beats
    )
    if meme_edit_enabled() and (meme_captions or meme_beats) and not _use_moviepy():
        logger.warning("%s Meme 特效需 HONGGUO_VIDEO_BACKEND=moviepy，已跳过梗字幕/卡点", label)
    if _use_moviepy():
        try:
            mpy_process_body_clip(
                raw,
                dest,
                label,
                seconds,
                trim_start_sec=trim_start,
                max_duration_sec=max_duration_sec,
                speed=speed,
                commentary_lines=body_lines,
                originality_seed=originality_seed,
                work_dir=work_dir,
                need_originality=need_originality,
                meme_captions=meme_captions,
                meme_beats=meme_beats,
                clip_tail_pad=clip_tail_pad,
                soft_audio_fade_out=soft_audio_fade_out,
                soft_audio_fade_in=soft_audio_fade_in,
            )
            dur = _probe_duration(dest)
            if dur < 0.5:
                raise RuntimeError(f"{label} 正片时长异常（{dur:.1f}s）")
            use_speed = abs(speed - 1.0) >= 0.01
            output_clip_len = (
                float(max_duration_sec) if max_duration_sec and max_duration_sec > 0 else 0.0
            )
            source_clip_len = (
                output_clip_len * speed if use_speed and output_clip_len > 0.01 else output_clip_len
            )
            clip_note = ""
            if trim_start > 0.01 or output_clip_len > 0.01:
                target = output_clip_len or dur
                clip_note = f"（起点 {trim_start:.1f}s，成片 {target:.1f}s"
                if use_speed:
                    clip_note += f"，{speed:g}x，源片约 {source_clip_len:.1f}s"
                clip_note += "）"
            elif use_speed:
                clip_note = f"（{speed:g}x 倍速）"
            meme_tag = ""
            if meme_captions or meme_beats:
                meme_tag = f"，Meme字幕{len(meme_captions)}条/卡点{len(meme_beats)}个"
            logger.info(
                "%s 正片 %.1fs%s（MoviePy%s，原声: %s）",
                label,
                dur,
                clip_note,
                meme_tag,
                "是" if audio_is_audible(dest) else "否",
            )
            return
        except Exception as exc:
            logger.warning("%s MoviePy 正片失败，回退 FFmpeg: %s", label, exc)

    output_clip_len = (
        float(max_duration_sec) if max_duration_sec and max_duration_sec > 0 else 0.0
    )
    speed = body_playback_speed()
    use_speed = abs(speed - 1.0) >= 0.01
    source_clip_len = (
        output_clip_len * speed if use_speed and output_clip_len > 0.01 else output_clip_len
    )
    src_codec = _video_codec(raw)
    need_originality = originality_enabled()
    # 非 1x 倍速必须重编码；去重增强同样必须重编码
    use_copy_first = (
        not need_originality
        and not use_speed
        and (prefer_stream_copy or src_codec in ("hevc", "h265"))
    )

    overlay_pngs: list[Path] = []
    sub_lines: list[str] = []
    if (
        need_originality
        and body_burn_subtitles()
        and body_lines
        and work_dir
        and output_clip_len > 0.5
    ):
        sub_lines = [str(x).strip() for x in body_lines if str(x).strip()][:5]
        for i, line in enumerate(sub_lines):
            png = work_dir / f"{dest.stem}_ov_{i}.png"
            render_subtitle_overlay_png(
                png, line, width=WORK_WIDTH, height=WORK_HEIGHT
            )
            overlay_pngs.append(png)

    def _run_copy() -> None:
        copy_args = ["-hide_banner", "-loglevel", "error"]
        if trim_start > 0.01:
            copy_args.extend(["-ss", f"{trim_start:.3f}"])
        copy_args.extend(["-i", str(raw)])
        if source_clip_len > 0.01:
            copy_args.extend(["-t", f"{source_clip_len:.3f}"])
        copy_args.extend(
            [
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
        _run_ffmpeg(
            copy_args, timeout=_ffmpeg_timeout(source_clip_len or seconds, base=600)
        )

    def _run_reencode() -> None:
        ffmpeg_args: list[str] = ["-hide_banner", "-loglevel", "error"]
        if trim_start > 0.01:
            ffmpeg_args.extend(["-ss", f"{trim_start:.3f}"])
        ffmpeg_args.extend(["-i", str(raw)])
        if source_clip_len > 0.01:
            ffmpeg_args.extend(["-t", f"{source_clip_len:.3f}"])
        for png in overlay_pngs:
            ffmpeg_args.extend(["-loop", "1", "-i", str(png)])

        base_vf = _body_vf_with_speed(
            speed,
            seed_str=f"{originality_seed}:{label}",
        )
        if overlay_pngs:
            fc, vout = build_subtitle_overlay_filter_complex(
                base_vf=base_vf,
                overlay_count=len(overlay_pngs),
                duration_sec=output_clip_len or source_clip_len or seconds,
                lines_count=len(sub_lines),
            )
            ffmpeg_args.extend(
                [
                    "-filter_complex",
                    fc,
                    "-map",
                    f"[{vout}]",
                    "-map",
                    "0:a:0?",
                ]
            )
        else:
            ffmpeg_args.extend(["-vf", base_vf])

        ffmpeg_args.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                BODY_ENCODE_PRESET if need_originality else ENCODE_PRESET,
                "-crf",
                str(OUTPUT_CRF),
                "-g",
                str(OUTPUT_FPS),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                str(AUDIO_RATE),
                "-ac",
                "2",
            ]
        )
        atempo = _atempo_chain(speed)
        af_parts: list[str] = []
        if atempo:
            af_parts.append(atempo)
        if need_originality and not authentic_preservation_enabled():
            af_parts.append("highpass=f=80,lowpass=f=12000,volume=0.98")
        if af_parts:
            ffmpeg_args.extend(["-af", ",".join(af_parts)])
        if overlay_pngs:
            ffmpeg_args.append("-shortest")
        ffmpeg_args.extend(
            [
                *ffmpeg_metadata_strip_args(),
                "-movflags",
                "+faststart",
                str(dest),
            ]
        )
        _run_ffmpeg(
            ffmpeg_args, timeout=_ffmpeg_timeout(source_clip_len or seconds, base=600)
        )

    if use_copy_first:
        try:
            _run_copy()
        except RuntimeError as exc:
            logger.warning("%s 流复制失败，尝试重编码: %s", label, exc)
            _run_reencode()
    else:
        try:
            _run_reencode()
        except RuntimeError as exc:
            if use_speed:
                raise RuntimeError(
                    f"{label} 重编码失败（{speed:g}x 倍速无法回退原速）: {exc}"
                ) from exc
            logger.warning("%s 重编码失败，改流复制: %s", label, exc)
            _run_copy()

    if not dest.is_file() or dest.stat().st_size < 10_000:
        raise RuntimeError(f"{label} 正片处理失败")

    dur = _probe_duration(dest)
    if dur < 0.5:
        raise RuntimeError(f"{label} 正片时长异常（{dur:.1f}s）")

    clip_note = ""
    if trim_start > 0.01 or output_clip_len > 0.01:
        target = output_clip_len or dur
        clip_note = f"（起点 {trim_start:.1f}s，成片 {target:.1f}s"
        if use_speed:
            clip_note += f"，{speed:g}x，源片约 {source_clip_len:.1f}s"
        clip_note += "）"
    elif use_speed:
        clip_note = f"（{speed:g}x 倍速）"
    logger.info(
        "%s 正片 %.1fs%s（原声: %s，编码: %s）",
        label,
        dur,
        clip_note,
        "有" if _has_audio(dest) else "无",
        _video_codec(dest) or "unknown",
    )
    _ensure_output_aspect(dest, label)


async def _download_episode_segment_from_fq_koc(
    client: httpx.AsyncClient,
    dest: Path,
    label: str,
    seconds: float,
    *,
    series_id: str,
    item_id: str,
    drama_title: str,
    work_dir: Path,
    segment_plan: Optional[BodySegmentPlan] = None,
    commentary_lines: Optional[list[str]] = None,
    originality_seed: str = "",
) -> str:
    meta = await fetch_fq_koc_episode(
        client,
        book_id=series_id,
        item_id=item_id,
        drama_title=drama_title,
        dest_dir=work_dir / "fq_koc",
    )
    local = Path(meta["local_path"])
    clip_seconds = seconds
    from_local = meta.get("source") == "fq_koc_local"
    _clip_with_plan(
        local,
        dest,
        label,
        clip_seconds,
        segment_plan,
        prefer_stream_copy=from_local or _video_codec(local) in ("hevc", "h265"),
        commentary_lines=commentary_lines,
        originality_seed=originality_seed,
        work_dir=work_dir,
    )
    _ensure_segment_h264(dest, label)
    src = "本地缓存" if from_local else "达人中心"
    extras: list[str] = []
    if originality_enabled():
        extras.append("去重增强")
    if tts_enabled() and edge_tts_available():
        extras.append("TTS解说")
    extra = ("，" + "、".join(extras)) if extras else ""
    return f"{src}明文（{label}，含原声{extra}）"


async def _download_episode_segment_from_external(
    client: httpx.AsyncClient,
    dest: Path,
    label: str,
    seconds: float,
    *,
    drama_title: str,
    promo_keyword: str,
    share_url: str,
    work_dir: Path,
    segment_plan: Optional[BodySegmentPlan] = None,
    commentary_lines: Optional[list[str]] = None,
    originality_seed: str = "",
) -> str:
    meta = await fetch_external_body_source(
        client,
        drama_title=drama_title,
        keyword=promo_keyword,
        share_url=share_url,
        dest_dir=work_dir / "external",
    )
    local = Path(meta["local_path"])
    clip_seconds = seconds
    if meta.get("duration") and float(meta["duration"]) > 1:
        clip_seconds = min(seconds, float(meta["duration"]))
    _clip_with_plan(
        local,
        dest,
        label,
        clip_seconds,
        segment_plan,
        commentary_lines=commentary_lines,
        originality_seed=originality_seed,
        work_dir=work_dir,
    )
    cap = str(meta.get("caption") or "")[:40]
    platform = "抖音" if meta.get("source") == "douyin" else "快手"
    return f"{platform}素材（{cap}，含原声）"


def _card_to_video(
    image: Path,
    dest: Path,
    duration: float,
    *,
    ken_burns: bool = False,
    narration_text: str = "",
    work_dir: Optional[Path] = None,
    narration_voice: Optional[str] = None,
) -> None:
    """片头/片尾：直接输出 H.264，浏览器可预览。"""
    if _use_moviepy() and not ken_burns:
        try:
            mpy_image_to_video(
                image,
                dest,
                duration,
                fps=OUTPUT_FPS,
                narration_text=narration_text,
                work_dir=work_dir,
                narration_voice=narration_voice,
            )
            return
        except Exception as exc:
            logger.warning("MoviePy 卡片转视频失败，回退 FFmpeg: %s", exc)
    if ken_burns:
        frames = int(duration * OUTPUT_FPS)
        vf = (
            f"scale={WORK_WIDTH}:{WORK_HEIGHT},"
            f"zoompan=z='min(zoom+0.0008,1.06)':d={frames}:"
            f"s={WORK_WIDTH}x{WORK_HEIGHT}:fps={OUTPUT_FPS}"
        )
    else:
        vf = f"scale={WORK_WIDTH}:{WORK_HEIGHT},fps={OUTPUT_FPS}"
    _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(image),
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
            "-t",
            str(duration),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            ENCODE_PRESET,
            "-crf",
            str(OUTPUT_CRF),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        timeout=120,
    )


def _finalize_for_browser(src: Path, dest: Path) -> bool:
    """转 H.264 并锁定横屏 16:9（1920×1080），便于浏览器预览。"""
    dest.unlink(missing_ok=True)
    dur = _probe_duration(src) or 120.0
    try:
        _normalize_segment(src, dest, seconds=dur)
        return dest.is_file() and _video_codec(dest) == "h264"
    except RuntimeError:
        pass
    proc = subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "videotoolbox",
            "-i",
            str(src),
            "-vf",
            _video_scale_pad_filter(),
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "5M",
            "-maxrate",
            "6M",
            "-bufsize",
            "12M",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if not dest.is_file() or dest.stat().st_size < 200_000:
        return False
    if _video_codec(dest) != "h264":
        return False
    dur = _probe_duration(dest)
    if dur < 3:
        return False
    if proc.returncode != 0:
        logger.warning("浏览器转码 ffmpeg 返回 %s，但输出可用", proc.returncode)
    logger.info(
        "已转为浏览器可播 H.264，%.1fs，%.1f MB",
        dur,
        dest.stat().st_size / 1024 / 1024,
    )
    return True


def _bsf_for_codec(codec: str) -> str:
    if codec in ("hevc", "h265"):
        return "hevc_mp4toannexb"
    return "h264_mp4toannexb"


def _merge_via_mpegts(parts: list[Path], dest: Path) -> None:
    """混编码/混分辨率片段用 MPEG-TS 中转再封装为 MP4。"""
    dest.unlink(missing_ok=True)
    ts_files: list[Path] = []

    try:
        for i, part in enumerate(parts):
            ts_path = dest.parent / f"seg_{i}.ts"
            codec = _video_codec(part)
            bsf_attempts: list[list[str]] = []
            if codec in ("hevc", "h265"):
                bsf_attempts.append(["-bsf:v", "hevc_mp4toannexb"])
            elif codec == "h264":
                bsf_attempts.append(["-bsf:v", "h264_mp4toannexb"])
            bsf_attempts.append([])

            last_err: Optional[Exception] = None
            for bsf_args in bsf_attempts:
                try:
                    _run_ffmpeg(
                        [
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            str(part),
                            "-c",
                            "copy",
                            *bsf_args,
                            "-bsf:a",
                            "aac_adtstoasc",
                            "-f",
                            "mpegts",
                            str(ts_path),
                        ],
                        timeout=180,
                    )
                    last_err = None
                    break
                except RuntimeError as exc:
                    last_err = exc
                    ts_path.unlink(missing_ok=True)
            if last_err:
                raise RuntimeError(f"片段 {part.name} 转 TS 失败") from last_err
            if not ts_path.is_file() or ts_path.stat().st_size < 5000:
                raise RuntimeError(f"片段 {part.name} 转 TS 失败")
            ts_files.append(ts_path)

        concat_uri = "concat:" + "|".join(p.as_posix() for p in ts_files)
        _run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                concat_uri,
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            timeout=300,
        )
    finally:
        for ts in ts_files:
            ts.unlink(missing_ok=True)


def _merge_segments(
    parts: list[Path],
    dest: Path,
    *,
    body_seconds: float,
    intro_seconds: float = 0.0,
) -> None:
    dest.unlink(missing_ok=True)
    parts_total = sum(max(0.0, _probe_duration(p) or 0.0) for p in parts)
    # 以各片段实测时长为准校验（AI 计划时长常大于实际裁切结果）
    expected_min = max(2.5, parts_total * 0.82)

    if _use_moviepy():
        try:
            mpy_merge_video_files(parts, dest)
            dur = _probe_duration(dest)
            if not dest.is_file() or dest.stat().st_size < MIN_OUTPUT_BYTES:
                raise RuntimeError("MoviePy 合成文件过小")
            if dur < expected_min:
                raise RuntimeError(
                    f"合成视频时长异常（成片 {dur:.1f}s，片段合计约 {parts_total:.1f}s）"
                )
            logger.info(
                "拼接成功 %.1fs，%.1f MB（MoviePy）",
                dur,
                dest.stat().st_size / 1024 / 1024,
            )
            return
        except Exception as exc:
            logger.warning("MoviePy 拼接失败，回退 FFmpeg: %s", exc)
            dest.unlink(missing_ok=True)

    codecs = {_video_codec(p) for p in parts}
    mixed = len({c for c in codecs if c}) > 1

    def _try_concat_copy() -> None:
        list_file = dest.parent / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in parts),
            encoding="utf-8",
        )
        _run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            timeout=300,
        )

    if not mixed:
        _try_concat_copy()

    low_bitrate = (
        dest.is_file()
        and _video_bitrate_bps(dest) < 200_000
        and dest.stat().st_size < MIN_OUTPUT_BYTES * 3
    )
    if mixed or not dest.is_file() or dest.stat().st_size < MIN_OUTPUT_BYTES or low_bitrate:
        if mixed or low_bitrate:
            logger.warning("片段编码不一致或码率过低，改用 MPEG-TS 拼接")
        else:
            logger.warning("无损拼接失败，尝试 MPEG-TS 中转")
        dest.unlink(missing_ok=True)
        _merge_via_mpegts(parts, dest)

    if not dest.is_file() or dest.stat().st_size < MIN_OUTPUT_BYTES:
        raise RuntimeError("视频合成失败（文件过小或损坏），请重试或换一集")

    dur = _probe_duration(dest)
    if dur < expected_min:
        raise RuntimeError(
            f"合成视频时长异常（成片 {dur:.1f}s，片段合计约 {parts_total:.1f}s），"
            "可能拼接丢段，请重试"
        )
    if _video_bitrate_bps(dest) < 200_000:
        logger.warning("拼接后视频码率过低，整段转 H.264")
        fixed = dest.parent / f"{dest.stem}_fixed.mp4"
        if _finalize_for_browser(dest, fixed):
            dest.unlink(missing_ok=True)
            fixed.rename(dest)
    logger.info("拼接成功 %.1fs，%.1f MB", dur, dest.stat().st_size / 1024 / 1024)


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", title).strip() or "hook"
    return name[:40]


async def generate_hook_video(
    client: httpx.AsyncClient,
    *,
    series_id: str,
    drama_title: str,
    opening: str = "",
    keyword: str = "",
    cover_url: str = "",
    episode_item_ids: list[str],
    episode_titles: Optional[dict[str, str]] = None,
    use_kuaishou_material: bool = False,
    kuaishou_share_url: str = "",
    use_fq_koc_material: bool = True,
    use_ai_edit: bool = True,
    drama_intro: str = "",
    splash_title_font: Optional[int] = None,
    splash_subtitle_font: Optional[int] = None,
    splash_badge: str = "",
) -> tuple[Path, str, str, dict]:
    if not episode_item_ids:
        raise ValueError("请至少选择一集")
    if len(episode_item_ids) > MAX_EPISODES:
        raise ValueError(f"最多选择 {MAX_EPISODES} 集")

    work = Path(tempfile.mkdtemp(prefix="hongguo_hook_"))
    tts_note = ""
    if tts_enabled() and edge_tts_available():
        tts_note = f"，TTS={pick_voice_for_episode(series_id)}"
    body_seconds_total = 0.0
    episode_labels_pre: list[str] = []
    episode_durations_pre: list[float] = []
    for index, item_id in enumerate(episode_item_ids, start=1):
        episode_labels_pre.append(
            (episode_titles or {}).get(item_id) or f"第{index}集"
        )
        dur_local = local_material_duration(series_id, item_id)
        if dur_local <= 1:
            mat = find_local_material(series_id, item_id)
            if mat:
                dur_local = _probe_duration(mat)
        if dur_local <= 1:
            logger.warning(
                "第%d集素材时长探测失败，AI 校验将暂用 120s 占位：%s",
                index,
                item_id,
            )
        episode_durations_pre.append(dur_local if dur_local > 1 else 120.0)

    episode_transcripts: dict = {}
    episode_visual_profiles: dict = {}
    if use_ai_edit:
        import asyncio

        from video_transcript import asr_enabled, gather_episode_transcripts
        from video_moment_profile import (
            gather_episode_visual_profiles,
            visual_profile_enabled,
        )

        async def _gather_tx() -> dict:
            if not asr_enabled():
                return {}
            return await asyncio.to_thread(
                gather_episode_transcripts,
                series_id=series_id,
                episode_item_ids=episode_item_ids,
                episode_labels=episode_labels_pre,
                work_dir=work,
            )

        async def _gather_vis() -> dict:
            if not visual_profile_enabled():
                return {}
            return await asyncio.to_thread(
                gather_episode_visual_profiles,
                series_id=series_id,
                episode_item_ids=episode_item_ids,
                episode_labels=episode_labels_pre,
                work_dir=work,
            )

        episode_transcripts, episode_visual_profiles = await asyncio.gather(
            _gather_tx(), _gather_vis()
        )
        if episode_transcripts:
            from video_transcript import write_transcript_sidecar

            total_cues = 0
            for ep_idx, cues in episode_transcripts.items():
                total_cues += len(cues)
                write_transcript_sidecar(
                    cues, work / f"transcript_ep{ep_idx:02d}_full.txt"
                )
            logger.info(
                "已抽取 %d 集对白（共 %d 条），供 AI 分镜",
                len(episode_transcripts),
                total_cues,
            )
        elif use_ai_edit:
            logger.warning(
                "未得到对白时间轴：请 pip install -r server/requirements-asr.txt "
                "并确认 HONGGUO_ASR_ENABLED=1"
            )
        if episode_visual_profiles:
            total_moments = sum(len(v) for v in episode_visual_profiles.values())
            logger.info(
                "已分析 %d 集画面/音效高能轴（共 %d 段），供 AI 选打斗与快切",
                len(episode_visual_profiles),
                total_moments,
            )

        episode_edit_briefs: dict = {}
        try:
            from hook_timeline import body_main_sec

            body_tgt = body_main_sec()
        except ImportError:
            body_tgt = 50.0
        from episode_edit_brief import gather_episode_edit_briefs
        from video_keyframes import gather_episode_keyframes, vision_frames_enabled
        from qwen_client import is_likely_vision_model

        async def _gather_brief() -> dict:
            if not (episode_transcripts or episode_visual_profiles):
                return {}
            return await asyncio.to_thread(
                gather_episode_edit_briefs,
                episode_labels=episode_labels_pre,
                episode_durations=episode_durations_pre,
                episode_transcripts=episode_transcripts or None,
                episode_visual_profiles=episode_visual_profiles or None,
                body_target_sec=body_tgt,
                work_dir=work,
            )

        async def _gather_kf() -> dict:
            if not (vision_frames_enabled() and is_likely_vision_model()):
                return {}
            return await asyncio.to_thread(
                gather_episode_keyframes,
                series_id=series_id,
                episode_item_ids=episode_item_ids,
                episode_labels=episode_labels_pre,
                episode_durations=episode_durations_pre,
                work_dir=work,
                episode_visual_profiles=episode_visual_profiles or None,
                episode_transcripts=episode_transcripts or None,
            )

        episode_edit_briefs, episode_keyframes = await asyncio.gather(
            _gather_brief(), _gather_kf()
        )
        if episode_keyframes:
            n_kf = sum(len(v) for v in episode_keyframes.values())
            logger.info(
                "已抽取 %d 张关键帧，将以多模态方式发给视觉模型（%s）",
                n_kf,
                os.getenv("QWEN_MODEL", "").strip() or "default",
            )
        else:
            logger.info(
                "纯文本分镜（未传关键帧）；无对白段由本地画面/音效轴程序选段"
            )

        edit_plan = await plan_hook_edit(
            client,
            drama_title=drama_title,
            drama_intro=drama_intro,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels_pre,
            episode_durations=episode_durations_pre,
            episode_transcripts=episode_transcripts or None,
            episode_visual_profiles=episode_visual_profiles or None,
            episode_keyframes=episode_keyframes or None,
            episode_edit_briefs=episode_edit_briefs or None,
        )
    else:
        edit_plan = default_plan(
            drama_title=drama_title,
            opening=opening,
            keyword=keyword,
            episode_labels=episode_labels_pre,
            episode_durations=episode_durations_pre,
        )

    logger.info(
        "成片：%s | %s | 风格=%s | %s%s",
        video_backend(),
        "AI 剪辑大师" if use_ai_edit else "规则剪辑",
        edit_plan.edit_style,
        (edit_plan.hook_summary or "")[:80],
        tts_note,
    )

    use_pro = pro_60_template_enabled()
    splash_keyword = (keyword or "").strip() or drama_title.strip()
    promo_keyword = splash_keyword or edit_plan.outro_keyword
    fixed_opening = fixed_opening_text()
    if use_pro:
        edit_plan.opening_text = fixed_opening_line()
        edit_plan.commentary_lines = []
        fixed_opening = edit_plan.opening_text
        from hook_timeline import ai_body_faithful_enabled

        logger.info(timeline_summary())
        if ai_body_faithful_enabled():
            try:
                from hook_timeline import story_first_edit_enabled

                if story_first_edit_enabled():
                    logger.info(
                        "正片剪辑：故事完整优先（AI 据完整对白表选情节，时长由 clips 之和决定）"
                    )
                else:
                    logger.info(
                        "正片剪辑：严格按 AI 分镜（不压 body_main_sec、不对白压缩/前重后轻配方）"
                    )
            except ImportError:
                logger.info(
                    "正片剪辑：严格按 AI 分镜（不压 body_main_sec、不对白压缩/前重后轻配方）"
                )
    elif fixed_opening:
        edit_plan.opening_text = fixed_opening
        edit_plan.commentary_lines = []
    commentary_lines: list[str] = []
    if not fixed_opening:
        commentary_lines = pick_commentary_lines(
            plan_lines=edit_plan.commentary_lines,
            opening_text=edit_plan.opening_text,
            subtitle_hint=edit_plan.subtitle_hint,
            hook_summary=edit_plan.hook_summary,
            drama_title=drama_title,
            max_lines=3,
            meme_mode=meme_edit_enabled(),
        )
    ep_count = len(episode_item_ids)
    if hook_budget_enabled(ep_count):
        budget_note = budget_summary(
            ep_count,
            with_commentary=bool(
                originality_enabled() and edit_plan.opening_text.strip()
            ),
        )
        if budget_note:
            logger.info(budget_note)
    originality_seed = f"{series_id}:{splash_keyword}:{promo_keyword}"

    try:
        segments: list[Path] = []
        intro_seconds = 0.0

        golden_src: Optional[Path] = None
        body_paths: list[Path] = []

        splash_segment: Optional[Path] = None
        if splash_keyword:
            splash_img = work / "00_splash.png"
            splash_mp4 = work / "00_splash.mp4"
            render_keyword_splash_card(
                splash_img,
                splash_keyword,
                title_font_px=splash_title_font,
                subtitle_font_px=splash_subtitle_font,
                badge_text=splash_badge,
            )
            _card_to_video(
                splash_img, splash_mp4, KEYWORD_SPLASH_SECONDS, ken_burns=False
            )
            splash_norm = work / "00_splash_norm.mp4"
            _normalize_segment(
                splash_mp4, splash_norm, seconds=KEYWORD_SPLASH_SECONDS
            )
            splash_mp4.unlink(missing_ok=True)
            splash_segment = splash_norm
            if not use_pro:
                segments.append(splash_norm)
                intro_seconds = KEYWORD_SPLASH_SECONDS
            logger.info(
                "已添加关键词片头 %.1fs：%s",
                KEYWORD_SPLASH_SECONDS,
                _format_splash_title(splash_keyword),
            )

        if (
            not use_pro
            and originality_enabled()
            and edit_plan.opening_text.strip()
        ):
            comm_img = work / "00b_commentary.png"
            comm_mp4 = work / "00b_commentary.mp4"
            opening_line = edit_plan.opening_text.strip()
            opening_card_sec = COMMENTARY_CARD_SECONDS
            narr_voice = resolve_voice(
                os.getenv("HONGGUO_TTS_VOICE", "").strip()
                or pick_voice_for_episode(originality_seed)
            )
            if fixed_opening and tts_enabled() and edge_tts_available():
                _opening_mp3, opening_card_sec = opening_card_duration_for_text(
                    opening_line,
                    work,
                    voice=narr_voice,
                    cache_dir=TTS_CACHE_DIR,
                )
                logger.info(
                    "片头口播 TTS %.1fs → 卡片 %.1fs：%s",
                    probe_media_duration(_opening_mp3),
                    opening_card_sec,
                    opening_line,
                )
            render_commentary_card(
                comm_img,
                opening_line,
                drama_title=drama_title,
                minimal=bool(fixed_opening),
            )
            _card_to_video(
                comm_img,
                comm_mp4,
                opening_card_sec,
                ken_burns=False,
                narration_text=opening_line,
                work_dir=work,
                narration_voice=narr_voice,
            )
            comm_norm = work / "00b_commentary_norm.mp4"
            _normalize_segment(comm_mp4, comm_norm, seconds=opening_card_sec)
            comm_mp4.unlink(missing_ok=True)
            segments.append(comm_norm)
            intro_seconds += opening_card_sec
            logger.info("已添加片头口播卡 %.1fs", opening_card_sec)

        body_notes: list[str] = []
        from ai_edit_planner import hook_plan_clip_variant, plan_has_dialogue_full_variant
        from edge_tts_narration import dual_dialogue_export_enabled

        render_runs: list[tuple[str, HookEditPlan, str, str]] = [
            ("", edit_plan, "hook_merged.mp4", "hook_output.mp4"),
        ]
        from hook_timeline import ai_body_faithful_enabled

        if (
            dual_dialogue_export_enabled()
            and not ai_body_faithful_enabled()
            and plan_has_dialogue_full_variant(edit_plan)
        ):
            render_runs.append(
                (
                    "_full",
                    hook_plan_clip_variant(edit_plan, "full"),
                    "hook_merged_full.mp4",
                    "hook_output_完整对白.mp4",
                )
            )
            logger.info("对白双版本：将同时导出压缩版与完整对白版")

        outro_norm_pro: Optional[Path] = None
        outro_dur_pro = 0.0
        narr_voice_pro = resolve_voice(
            os.getenv("HONGGUO_TTS_VOICE", "").strip()
            or pick_voice_for_episode(originality_seed)
        )
        if use_pro:
            outro_line = fixed_outro_line()
            outro_img = work / "99_outro_cta.png"
            outro_mp4 = work / "99_outro_cta_raw.mp4"
            outro_norm_pro = work / "99_outro_cta.mp4"
            render_outro_card(outro_img, "", cta_line=outro_line)
            outro_dur_pro = outro_cta_sec()
            if tts_enabled() and edge_tts_available():
                from edge_tts_narration import opening_card_duration_for_text

                _, outro_dur_pro = opening_card_duration_for_text(
                    outro_line,
                    work,
                    voice=narr_voice_pro,
                    cache_dir=TTS_CACHE_DIR,
                )
                outro_dur_pro = max(outro_cta_sec(), min(6.0, outro_dur_pro))
            _card_to_video(
                outro_img,
                outro_mp4,
                outro_dur_pro,
                ken_burns=False,
                narration_text=outro_line,
                work_dir=work,
                narration_voice=narr_voice_pro,
            )
            _normalize_segment(outro_mp4, outro_norm_pro, seconds=outro_dur_pro)
            outro_mp4.unlink(missing_ok=True)

        primary_output: Optional[Path] = None

        for file_tag, plan_run, merged_name, output_name in render_runs:
            logger.info("开始合成成片…")
            body_paths = []
            golden_src = None
            body_seconds_total = 0.0
            intro_seconds = 0.0

            for index, item_id in enumerate(episode_item_ids, start=1):
                label = episode_labels_pre[index - 1]
                seg_plan = plan_run.body_for_index(index)
                if (
                    use_pro
                    and index == 1
                    and file_tag == ""
                    and not ai_body_faithful_enabled()
                ):
                    seg_plan.duration_sec += golden_open_sec()
                ep_seconds = episode_durations_pre[index - 1]
                clip_path = work / f"{index:02d}_body{file_tag}.mp4"
                note = ""
                fq_err: Optional[Exception] = None
                episode_ready = False

                if use_fq_koc_material:
                    has_local = bool(find_local_material(series_id, item_id))
                    if not has_local and not fq_koc_configured():
                        try:
                            raise_material_download_error(
                                book_id=series_id, item_id=item_id
                            )
                        except RuntimeError as exc:
                            fq_err = exc
                    else:
                        try:
                            note = await _download_episode_segment_from_fq_koc(
                                client,
                                clip_path,
                                label,
                                ep_seconds,
                                series_id=series_id,
                                item_id=item_id,
                                drama_title=drama_title,
                                work_dir=work,
                                segment_plan=seg_plan,
                                commentary_lines=commentary_lines,
                                originality_seed=f"{originality_seed}:{item_id}",
                            )
                            episode_ready = True
                        except Exception as exc:
                            fq_err = exc
                            logger.warning("推广中心素材失败 %s: %s", label, exc)

                if not episode_ready and use_kuaishou_material:
                    try:
                        note = await _download_episode_segment_from_external(
                            client,
                            clip_path,
                            label,
                            ep_seconds,
                            drama_title=drama_title,
                            promo_keyword=promo_keyword,
                            share_url=kuaishou_share_url,
                            work_dir=work,
                            segment_plan=seg_plan,
                            commentary_lines=commentary_lines,
                            originality_seed=f"{originality_seed}:ext",
                        )
                        episode_ready = True
                    except Exception as exc:
                        logger.warning("站外素材（快手/抖音）失败 %s: %s", label, exc)
                        if fq_err:
                            raise RuntimeError(
                                f"推广中心与站外素材均失败。推广中心: {fq_err}"
                            ) from exc
                        raise RuntimeError(f"站外素材失败: {exc}") from exc

                if not episode_ready:
                    if fq_err:
                        raise fq_err
                    raise RuntimeError(
                        "未启用推广中心素材。请勾选「使用推广中心素材」并配置 Cookie。"
                    )

                if not clip_path.is_file() or clip_path.stat().st_size < 10_000:
                    raise RuntimeError(f"{label} 素材文件未生成或过小，请重试下载")

                clip_dur = _probe_duration(clip_path) or 0.0
                body_seconds_total += clip_dur
                body_notes.append(note)
                body_paths.append(clip_path)
                if use_pro and index == 1 and golden_src is None:
                    golden_src = work / f"00_golden_src{file_tag}.mp4"
                    _extract_leading_clip(clip_path, golden_src, golden_open_sec())

            if use_pro:
                from hook_pro_render import (
                    build_freeze_segment,
                    enhance_golden_opening_clip,
                )

                pro_segments: list[Path] = []
                if splash_segment and splash_segment.is_file():
                    pro_segments.append(splash_segment)
                if golden_src and golden_src.is_file():
                    golden_final = work / f"00_golden{file_tag}.mp4"
                    enhance_golden_opening_clip(
                        golden_src,
                        opening_text=fixed_opening_line(),
                        work_dir=work,
                        voice=narr_voice_pro,
                        dest=golden_final,
                    )
                    pro_segments.append(golden_final)
                    intro_seconds = _probe_duration(golden_final) or golden_open_sec()
                    if body_paths:
                        golden_dur = intro_seconds
                        trimmed = work / f"01_body_nogolden{file_tag}.mp4"
                        _trim_leading_clip(body_paths[0], trimmed, golden_dur)
                        body_paths[0].unlink(missing_ok=True)
                        trimmed.rename(body_paths[0])
                        body_seconds_total = sum(
                            _probe_duration(p) or 0.0 for p in body_paths
                        )
                elif body_paths:
                    golden_src = work / f"00_golden_src{file_tag}.mp4"
                    _extract_leading_clip(
                        body_paths[0], golden_src, golden_open_sec()
                    )
                    golden_final = work / f"00_golden{file_tag}.mp4"
                    enhance_golden_opening_clip(
                        golden_src,
                        opening_text=fixed_opening_line(),
                        work_dir=work,
                        voice=narr_voice_pro,
                        dest=golden_final,
                    )
                    pro_segments.append(golden_final)
                    intro_seconds = _probe_duration(golden_final) or golden_open_sec()
                    golden_dur = intro_seconds
                    trimmed = work / f"01_body_nogolden{file_tag}.mp4"
                    _trim_leading_clip(body_paths[0], trimmed, golden_dur)
                    body_paths[0].unlink(missing_ok=True)
                    trimmed.rename(body_paths[0])
                    body_seconds_total = sum(
                        _probe_duration(p) or 0.0 for p in body_paths
                    )

                pro_segments.extend(body_paths)
                freeze_mp4 = work / f"98_fadeout{file_tag}.mp4"
                from hook_duration_budget import hook_target_min_sec
                from hook_timeline import (
                    body_outro_skip_transition,
                    body_outro_use_fade,
                    freeze_hold_sec,
                )

                splash_dur_pre = (
                    _probe_duration(splash_segment) or KEYWORD_SPLASH_SECONDS
                    if splash_segment
                    else 0.0
                )
                golden_dur_pre = sum(
                    _probe_duration(p) or 0.0
                    for p in pro_segments
                    if "golden" in p.name
                )
                body_dur_pre = sum(_probe_duration(p) or 0.0 for p in body_paths)
                outro_dur_est = outro_dur_pro or outro_cta_sec()
                skip_tail = body_outro_skip_transition()
                fade_dur = 0.0 if skip_tail else freeze_hold_sec()
                min_total = hook_target_min_sec()
                projected = (
                    splash_dur_pre
                    + golden_dur_pre
                    + body_dur_pre
                    + fade_dur
                    + outro_dur_est
                )
                if (
                    not skip_tail
                    and projected < min_total - 0.25
                    and not ai_body_faithful_enabled()
                ):
                    fade_dur = min(3.0, fade_dur + (min_total - projected))
                if skip_tail:
                    logger.info("正片尾跳过黑场/淡出，直接接片尾口播")
                else:
                    freeze_src = body_paths[-1] if body_paths else pro_segments[-1]
                    build_freeze_segment(
                        freeze_src,
                        freeze_mp4,
                        work_dir=work,
                        duration=fade_dur,
                    )
                    pro_segments.append(freeze_mp4)
                if outro_norm_pro and outro_norm_pro.is_file():
                    pro_segments.append(outro_norm_pro)
                run_segments = pro_segments
                body_seconds_total = sum(
                    _probe_duration(p) or 0.0 for p in body_paths
                )
                splash_dur = (
                    _probe_duration(splash_segment) or KEYWORD_SPLASH_SECONDS
                    if splash_segment
                    else 0.0
                )
                fade_label = "跳过" if skip_tail else ("淡出" if body_outro_use_fade() else "定格")
                fade_dur_actual = 0.0 if skip_tail else (_probe_duration(freeze_mp4) or 0.0)
                seg0 = plan_run.body_for_index(1)
                plan_body = float(seg0.duration_sec) if seg0 else 0.0
                plan_clips = (
                    sum(c.duration_sec for c in seg0.clips) if seg0 and seg0.clips else 0.0
                )
                measured_total = (
                    splash_dur
                    + intro_seconds
                    + body_seconds_total
                    + fade_dur_actual
                    + outro_dur_pro
                )
                logger.info(
                    "专业60s 成片实测(ffprobe)：封面%.1fs + 黄金%.1fs + 正片%.1fs"
                    " + %s%.1fs + 尾帧%.1fs ≈ %.1fs | AI 计划正片 body=%.1fs clips=%.1fs",
                    splash_dur,
                    intro_seconds,
                    body_seconds_total,
                    fade_label,
                    fade_dur_actual,
                    outro_dur_pro,
                    measured_total,
                    plan_body,
                    plan_clips,
                )
            else:
                run_segments = list(segments)
                run_segments.extend(body_paths)

            if not use_pro and originality_outro_enabled() and promo_keyword:
                outro_img = work / f"99_outro{file_tag}.png"
                outro_mp4 = work / f"99_outro{file_tag}.mp4"
                render_outro_card(outro_img, promo_keyword)
                _card_to_video(
                    outro_img, outro_mp4, OUTRO_SEARCH_SECONDS, ken_burns=False
                )
                outro_norm = work / f"99_outro_norm{file_tag}.mp4"
                _normalize_segment(
                    outro_mp4, outro_norm, seconds=OUTRO_SEARCH_SECONDS
                )
                outro_mp4.unlink(missing_ok=True)
                run_segments.append(outro_norm)
                logger.info("已添加片尾搜索引导 %.1fs", OUTRO_SEARCH_SECONDS)

            merged = work / merged_name
            _merge_segments(
                run_segments,
                merged,
                body_seconds=body_seconds_total,
                intro_seconds=intro_seconds,
            )

            output = work / output_name
            need_transcode = (
                _video_codec(merged) in ("hevc", "h265")
                or _video_bitrate_bps(merged) < 200_000
            )
            if need_transcode:
                if not _finalize_for_browser(merged, output):
                    raise RuntimeError(
                        "合成视频无法在浏览器中播放，请检查推广中心素材是否完整后重试"
                    )
            elif video_decodes(merged):
                merged.rename(output)
            elif not _finalize_for_browser(merged, output):
                raise RuntimeError(
                    "合成视频无法在浏览器中播放，请检查推广中心素材是否完整后重试"
                )

            out_w, out_h = _probe_video_size(output)
            if out_w != WORK_WIDTH or out_h != WORK_HEIGHT:
                fixed = work / f"hook_final_169{file_tag}.mp4"
                _normalize_segment(
                    output, fixed, seconds=_probe_duration(output) or 120.0
                )
                output.unlink(missing_ok=True)
                fixed.rename(output)
                logger.info(
                    "成片已校正为横屏 %dx%d (%s)",
                    WORK_WIDTH,
                    WORK_HEIGHT,
                    ASPECT_LABEL,
                )

            if output.stat().st_size < MIN_OUTPUT_BYTES:
                raise RuntimeError("合成视频过小，剧集画面可能未成功写入")

            if not video_decodes(output):
                raise RuntimeError("成片无法解码播放，请重试或换一集")

            if watermark_enabled():
                wm_path = work / f"hook_watermarked{file_tag}.mp4"
                if burn_corner_watermark(output, wm_path):
                    output.unlink(missing_ok=True)
                    wm_path.rename(output)

            if not _has_audio(output):
                logger.warning("成片未检测到音轨")
            elif not audio_is_audible(output):
                logger.warning("成片音轨音量过低，听感可能接近无声")

            final_name = f"{_safe_filename(drama_title)}_钩子.mp4"
            primary_output = output

        if primary_output is None:
            raise RuntimeError("未生成成片")
        output = primary_output

        warning = ""
        if body_notes and all("明文" in n for n in body_notes):
            tts_tip = (
                "Edge TTS 解说音轨（晓晓/晓伊/云阳）；"
                if tts_enabled() and edge_tts_available()
                else ""
            )
            if authentic_preservation_enabled():
                light = "轻度" if originality_light_visual() else ""
                budget_tip = ""
                if hook_budget_enabled(ep_count):
                    mc = "多段快切" if multi_clip_enabled() else "连续裁剪"
                    budget_tip = f"{ep_count}集{hook_duration_range_text()}（{mc}）；"
                wm_tip = (
                    f"角落水印「{watermark_text()}」；"
                    if watermark_enabled()
                    else ""
                )
                warning = (
                    f"横屏 {WORK_WIDTH}×{WORK_HEIGHT}（{ASPECT_LABEL}），"
                    f"{budget_tip}{wm_tip}"
                    f"正片原味（{body_playback_speed():g}x、原声对白、无正片解说条/meme），"
                    f"{light}像素去重"
                    f"{'+抖音合规片头（无站外导流）' if douyin_safe_enabled() else '+片头片尾引导'}。"
                )
            elif meme_edit_enabled() and edit_plan.edit_style == "meme":
                warning = (
                    f"横屏 {WORK_WIDTH}×{WORK_HEIGHT}（{ASPECT_LABEL}），"
                    f"{tts_tip}"
                    "Meme 风剪辑（AI 梗字幕+卡点缩放+1.618倍速+去重解说）。"
                )
            elif originality_enabled():
                warning = (
                    f"横屏 {WORK_WIDTH}×{WORK_HEIGHT}（{ASPECT_LABEL}），"
                    f"{tts_tip}"
                    "已启用抖音去重增强（解说字幕+微调色+片头片尾引导）。"
                )
            elif tts_enabled() and edge_tts_available():
                warning = (
                    f"横屏 {WORK_WIDTH}×{WORK_HEIGHT}（{ASPECT_LABEL}），"
                    f"{tts_tip}正片含 AI 解说配音。"
                )
            else:
                warning = (
                    f"横屏 {WORK_WIDTH}×{WORK_HEIGHT}（{ASPECT_LABEL}），"
                    "正片来自推广中心明文素材（含原声）。"
                )
        elif body_notes:
            warning = "；".join(body_notes[:2])
        if (
            use_ai_edit
            and edit_plan.hook_summary
            and "未配置 LLM" not in edit_plan.hook_summary
            and "未启用 AI" not in edit_plan.hook_summary
        ):
            ai_note = f" AI 剪辑：{edit_plan.hook_summary}"
            warning = (warning + ai_note) if warning else ai_note.strip()
        logger.info(
            "钩子生成完成 %s，%.1f MB，时长 %.1fs，含音频: %s，编码: %s",
            final_name,
            output.stat().st_size / 1024 / 1024,
            _probe_duration(output),
            _has_audio(output),
            _video_codec(output),
        )
        return output, final_name, warning, plan_to_dict(edit_plan)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
