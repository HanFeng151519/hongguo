"""站外素材（快手/抖音）公共工具。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from kuaishou_material import probe_decodes


def _ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for c in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(c).is_file():
            return c
    return "ffmpeg"


def is_usable_video_file(path: Path, *, min_bytes: int = 100_000) -> bool:
    """判断是否为可处理的视频文件（比 probe_decodes 宽松，兼容部分 HEVC）。"""
    if not path.is_file() or path.stat().st_size < min_bytes:
        return False
    proc = subprocess.run(
        [_ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    err = proc.stderr or ""
    return "Video:" in err and "Duration:" in err


def is_http_url(value: Any) -> bool:
    u = str(value or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def score_caption(caption: str, keyword: str, drama_title: str) -> int:
    text = (caption or "").lower()
    score = 0
    for term in (keyword, drama_title):
        t = (term or "").strip().lower()
        if not t:
            continue
        if t in text:
            score += 10
        for part in re.split(r"[\s之·#]+", t):
            if len(part) >= 2 and part in text:
                score += 3
    if any(x in text for x in ("短剧", "漫剧", "合集", "第")):
        score += 2
    return score


def apply_fanqie_vod_query(url: str) -> str:
    """合并 .env 中 HONGGUO_FQ_KOC_VOD_QUERY（F12 复制 CDN 参数，用于刷新 ft/dy_q 等）。"""
    template = os.getenv("HONGGUO_FQ_KOC_VOD_QUERY", "").strip().lstrip("?")
    if not template or "fanqieopenvod" not in url.lower() and "fqkol" not in url.lower():
        return url
    parsed = urlparse(url)
    merged = dict(parse_qsl(parsed.query, keep_blank_values=True))
    merged.update(dict(parse_qsl(template, keep_blank_values=True)))
    return urlunparse(parsed._replace(query=urlencode(merged)))


def _download_chunk_size() -> int:
    raw = os.getenv("HONGGUO_DOWNLOAD_CHUNK_MB", "2").strip()
    try:
        mb = max(1, min(16, int(raw)))
    except ValueError:
        mb = 2
    return mb * 1024 * 1024


async def download_http_video(
    client: httpx.AsyncClient,
    play_url: str,
    dest: Path,
    *,
    referer: str,
    max_bytes: int = 500_000_000,
) -> Path:
    if not is_http_url(play_url):
        raise RuntimeError("播放地址无效")
    play_url = apply_fanqie_vod_query(play_url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
    }
    total = 0
    async with client.stream(
        "GET", play_url, headers=headers, timeout=1800.0, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=_download_chunk_size()):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("素材超过大小限制")
                fh.write(chunk)
    if total < 50_000:
        raise RuntimeError("素材下载过小，可能链接已过期")
    return dest


def is_douyin_url(url: str) -> bool:
    u = url.lower()
    return any(
        x in u
        for x in (
            "douyin.com",
            "iesdouyin.com",
            "v.douyin.com",
            "douyin.cn",
        )
    )


def is_kuaishou_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("kuaishou.com", "kuaishou.cn", "chenzhongtech.com"))


def yt_dlp_download(url: str, dest: Path, *, cookie: str = "") -> Path:
    """用 yt-dlp 下载抖音/快手分享页（需 Cookie 时传入）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ytdlp = shutil.which("yt-dlp")
    if ytdlp:
        args = [
            ytdlp,
            "-o",
            str(dest),
            "--no-playlist",
            "--merge-output-format",
            "mp4",
        ]
    else:
        args = [
            "python3",
            "-m",
            "yt_dlp",
            "-o",
            str(dest),
            "--no-playlist",
            "--merge-output-format",
            "mp4",
        ]
    if cookie.strip():
        cookie_file = dest.parent / "_cookies.txt"
        cookie_file.write_text(cookie.strip(), encoding="utf-8")
        args.extend(["--cookies", str(cookie_file)])
    args.append(url)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        raise RuntimeError(f"yt-dlp 下载失败: {tail}")
    if dest.is_file():
        return dest
    candidates = sorted(dest.parent.glob(f"{dest.stem}*"))
    for c in candidates:
        if c.suffix in (".mp4", ".mkv", ".webm") and c.stat().st_size > 50_000:
            if c != dest:
                c.rename(dest)
            return dest
    raise RuntimeError("yt-dlp 未生成视频文件")


__all__ = [
    "download_http_video",
    "is_douyin_url",
    "is_http_url",
    "is_kuaishou_url",
    "probe_decodes",
    "score_caption",
    "yt_dlp_download",
]
