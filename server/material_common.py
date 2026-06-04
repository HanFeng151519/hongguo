"""站外素材（快手/抖音）公共工具。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from kuaishou_material import probe_decodes


def _ffmpeg_bin() -> str:
    from ffmpeg_util import resolve_ffmpeg_exe

    return resolve_ffmpeg_exe()


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


_DOUYIN_SHARE_URL_RE = re.compile(
    r"https?://(?:v\.douyin\.com|(?:www\.)?douyin\.com|(?:www\.)?iesdouyin\.com)[^\s\]\)\"'<>，。；;]+",
    re.IGNORECASE,
)


def extract_douyin_share_url(text: str) -> str:
    """从分享文案中提取抖音链接（支持整段粘贴）。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    m = _DOUYIN_SHARE_URL_RE.search(raw)
    if m:
        return m.group(0).rstrip("，。,.;；'\"")
    if is_douyin_url(raw):
        first = raw.split()[0] if raw.startswith("http") else raw
        return first.rstrip("，。,.;；'\"")
    return ""


def is_kuaishou_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("kuaishou.com", "kuaishou.cn", "chenzhongtech.com"))


def _yt_dlp_executable() -> list[str]:
    """本机 yt-dlp 启动命令（Windows 用当前 Python，不用 python3）。"""
    ytdlp = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if ytdlp:
        return [ytdlp]
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return []


def douyin_download_auth_configured() -> bool:
    if os.getenv("HONGGUO_DOUYIN_COOKIE", "").strip():
        return True
    if os.getenv("HONGGUO_DOUYIN_COOKIE_FILE", "").strip():
        p = Path(os.getenv("HONGGUO_DOUYIN_COOKIE_FILE", "").strip())
        if p.is_file():
            return True
    if os.getenv("HONGGUO_YTDLP_COOKIES_FROM_BROWSER", "").strip():
        return True
    return False


def _header_cookie_args(cookie: str, work_dir: Path) -> list[str]:
    c = cookie.strip()
    if not c:
        return []
    if c.lstrip().startswith("# Netscape") or (
        "\t" in c[:800] and ".douyin" in c.lower()
    ):
        cf = work_dir / "_douyin_cookies.txt"
        cf.write_text(c, encoding="utf-8")
        return ["--cookies", str(cf), "--add-header", "Referer:https://www.douyin.com/"]
    hdr = c if c.lower().startswith("cookie:") else f"Cookie:{c}"
    return ["--add-header", hdr, "--add-header", "Referer:https://www.douyin.com/"]


def _yt_dlp_auth_modes(work_dir: Path) -> list[tuple[str, list[str]]]:
    """
    鉴权尝试顺序（名称, yt-dlp 参数）。
    Windows 下 Chrome 开着时无法复制 Cookie DB，故优先用 .env 字符串，浏览器放后并含 Edge 回退。
    """
    modes: list[tuple[str, list[str]]] = []
    seen: set[str] = set()

    def add(name: str, args: list[str]) -> None:
        key = " ".join(args)
        if key and key not in seen:
            seen.add(key)
            modes.append((name, args))

    env_cookie = os.getenv("HONGGUO_DOUYIN_COOKIE", "").strip()
    if env_cookie:
        add("HONGGUO_DOUYIN_COOKIE", _header_cookie_args(env_cookie, work_dir))

    cookie_file = os.getenv("HONGGUO_DOUYIN_COOKIE_FILE", "").strip()
    if cookie_file:
        p = Path(cookie_file)
        if p.is_file():
            add("cookie_file", ["--cookies", str(p), "--add-header", "Referer:https://www.douyin.com/"])

    use_browser = os.getenv("HONGGUO_YTDLP_USE_BROWSER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    browser = os.getenv("HONGGUO_YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if browser and (use_browser or not env_cookie):
        add(f"browser:{browser}", ["--cookies-from-browser", *browser.split(":")])

    if use_browser and browser:
        fallback = os.getenv(
            "HONGGUO_YTDLP_COOKIES_FALLBACK",
            "edge" if sys.platform == "win32" and browser.lower().startswith("chrome") else "",
        ).strip()
        if fallback:
            fb_key = fallback.split(":")[0].lower()
            br_key = browser.split(":")[0].lower()
            if fb_key != br_key:
                add(
                    f"browser:{fallback}",
                    ["--cookies-from-browser", *fallback.split(":")],
                )

    if not modes:
        modes.append(("none", []))
    return modes


def _friendly_yt_dlp_error(stderr: str, stdout: str) -> str:
    blob = f"{stderr}\n{stdout}".lower()
    if "no module named yt_dlp" in blob or "not found" in blob and "yt-dlp" in blob:
        return (
            "未安装 yt-dlp。请在 server 目录执行："
            "py -3.12 -m pip install yt-dlp"
        )
    if "could not copy" in blob and "cookie" in blob:
        return (
            "无法从浏览器读取 Cookie（Windows 上 Chrome/Edge 开着时会被锁定）。\n"
            "请在本页「抖音 Cookie」框粘贴（推荐），或在 .env 设置 HONGGUO_DOUYIN_COOKIE=…，\n"
            "并注释掉 HONGGUO_YTDLP_COOKIES_FROM_BROWSER。"
        )
    if "fresh cookies" in blob or "cookies are needed" in blob:
        return (
            "抖音要求登录 Cookie。任选其一：\n"
            "1) .env 设置 HONGGUO_DOUYIN_COOKIE=浏览器 F12 复制的 Cookie 字符串（Windows 推荐）；\n"
            "2) .env 设置 HONGGUO_YTDLP_COOKIES_FROM_BROWSER=edge 或 chrome（chrome 需关闭浏览器）；\n"
            "3) 用扩展导出 Netscape cookies.txt，设置 HONGGUO_DOUYIN_COOKIE_FILE=文件路径"
        )
    tail = (stderr or stdout or "").strip()[-600:]
    return f"yt-dlp 下载失败：{tail or '未知错误'}"


def _yt_dlp_run(cmd: list[str], url: str, auth: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*cmd, *auth, url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def _yt_dlp_collect_output(dest: Path) -> Path:
    if dest.is_file() and dest.stat().st_size > 50_000:
        return dest
    for c in sorted(
        dest.parent.glob(f"{dest.stem}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if c.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov") and c.stat().st_size > 50_000:
            if c != dest:
                dest.unlink(missing_ok=True)
                c.rename(dest)
            return dest
    raise RuntimeError("yt-dlp 未生成视频文件，请检查 Cookie 或更换链接")


def yt_dlp_download(url: str, dest: Path, *, cookie: str = "") -> Path:
    """用 yt-dlp 下载抖音/快手分享页（多种 Cookie 方式依次尝试）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = _yt_dlp_executable()
    if not cmd:
        raise RuntimeError(
            "未安装 yt-dlp。请在 server 目录执行：py -3.12 -m pip install yt-dlp"
        )

    out_tpl = str(dest.with_suffix("")) + ".%(ext)s"
    base = [
        *cmd,
        "--no-playlist",
        "--no-warnings",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/b",
        "-o",
        out_tpl,
    ]

    modes = _yt_dlp_auth_modes(dest.parent)
    if cookie.strip() and not any(n == "HONGGUO_DOUYIN_COOKIE" for n, _ in modes):
        modes.insert(0, ("param_cookie", _header_cookie_args(cookie, dest.parent)))

    last_stderr = ""
    last_stdout = ""
    for _name, auth in modes:
        proc = _yt_dlp_run(base, url, auth)
        if proc.returncode == 0:
            return _yt_dlp_collect_output(dest)
        last_stderr = proc.stderr or ""
        last_stdout = proc.stdout or ""
        blob = f"{last_stderr}\n{last_stdout}".lower()
        if "could not copy" not in blob or "cookie" not in blob:
            break

    raise RuntimeError(_friendly_yt_dlp_error(last_stderr, last_stdout))


__all__ = [
    "download_http_video",
    "douyin_download_auth_configured",
    "extract_douyin_share_url",
    "is_douyin_url",
    "is_http_url",
    "is_kuaishou_url",
    "probe_decodes",
    "score_caption",
    "yt_dlp_download",
]
