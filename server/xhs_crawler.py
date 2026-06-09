"""小红书分享爬取：xhslink 短链 / 笔记页 → __INITIAL_STATE__ → 视频 MP4。"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from douyin_crawler import extract_all_http_urls

logger = logging.getLogger(__name__)

_XHS_HOSTS = (
    "xhslink.com",
    "xiaohongshu.com",
    "xhs.cn",
)

_XHS_SHARE_URL_RE = re.compile(
    r"https?://(?:www\.)?xhslink\.com/[^\s\]\)\"'<>，。；;]+|"
    r"https?://(?:www\.)?xiaohongshu\.com/[^\s\]\)\"'<>，。；;]+",
    re.IGNORECASE,
)

_NOTE_ID_RE = re.compile(
    r"/(?:discovery/item|explore)/([a-f0-9]{24})",
    re.IGNORECASE,
)

_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>",
    re.S,
)


def _xhs_cookie() -> str:
    return os.getenv("HONGGUO_XHS_COOKIE", "").strip()


def is_xhs_share_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _XHS_HOSTS)


def extract_xhs_share_url(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    m = _XHS_SHARE_URL_RE.search(raw)
    if m:
        return m.group(0).rstrip("，。,.;；'\"`")
    for u in extract_all_http_urls(raw):
        if is_xhs_share_url(u):
            return u
    return ""


def _note_id_from_url(url: str) -> str:
    m = _NOTE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _parse_initial_state(html: str) -> dict[str, Any]:
    m = _INITIAL_STATE_RE.search(html or "")
    if not m:
        raise RuntimeError("小红书页面未返回笔记数据")
    raw = m.group(1).replace("undefined", "null")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("小红书页面数据解析失败") from exc


def _score_video_url(url: str) -> int:
    u = url.lower()
    if ".mp4" not in u and "/stream/" not in u:
        return -100
    if "webpic" in u or u.endswith(".js") or "!nd_" in u:
        return -100
    score = 0
    if ".mp4" in u:
        score += 50
    if "sns-video" in u:
        score += 30
    if "/stream/" in u:
        score += 20
    if "sign=" in u:
        score += 5
    if "sns-bak" in u:
        score -= 10
    return score


def _collect_video_urls(state: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 16:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in ("masterUrl", "master_url"):
                    if isinstance(val, str) and val.startswith("http"):
                        found.append(val.replace("\\u002F", "/"))
                if key == "backupUrls" and isinstance(val, list):
                    for item in val:
                        if isinstance(item, str) and item.startswith("http"):
                            found.append(item.replace("\\u002F", "/"))
                if key == "stream" and isinstance(val, dict):
                    for codec_val in val.values():
                        if isinstance(codec_val, list):
                            for item in codec_val:
                                if isinstance(item, dict):
                                    u = item.get("masterUrl")
                                    if isinstance(u, str) and u.startswith("http"):
                                        found.append(u.replace("\\u002F", "/"))
                walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(state)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen and _score_video_url(u) >= 0:
            seen.add(u)
            out.append(u)
    out.sort(key=_score_video_url, reverse=True)
    return out


def _meta_from_state(state: dict[str, Any], note_id: str) -> dict[str, Any]:
    note_map = ((state.get("note") or {}).get("noteDetailMap")) or {}
    detail = note_map.get(note_id) or {}
    note = detail.get("note") or {}
    title = str(note.get("title") or note.get("desc") or "")[:200]
    duration_ms = 0.0
    video = note.get("video") or {}
    consumer = video.get("consumer") or {}
    origin = consumer.get("originVideoKey") or consumer.get("origin_video_key")
    if isinstance(origin, dict):
        duration_ms = float(origin.get("duration") or 0)
    stream = video.get("media") or video.get("mediaV2")
    if isinstance(stream, str):
        try:
            stream = json.loads(stream)
        except json.JSONDecodeError:
            stream = {}
    if isinstance(stream, dict):
        v = stream.get("video") or {}
        if isinstance(v, dict) and not duration_ms:
            duration_ms = float(v.get("duration") or 0)
    return {
        "caption": title,
        "duration": duration_ms / 1000.0 if duration_ms > 100 else 0.0,
        "note_type": str(note.get("type") or ""),
        "cover_url": str(note.get("cover", {}).get("urlDefault", "") if isinstance(note.get("cover"), dict) else ""),
    }


async def _fetch_note_page(
    client: httpx.AsyncClient, share_url: str
) -> tuple[str, str, dict[str, Any]]:
    headers = {
        "User-Agent": _PC_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    cookie = _xhs_cookie()
    if cookie:
        headers["Cookie"] = cookie
    resp = await client.get(
        share_url.strip(),
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    final = str(resp.url)
    note_id = _note_id_from_url(final) or _note_id_from_url(share_url)
    state = _parse_initial_state(resp.text or "")
    if not note_id:
        note_map = ((state.get("note") or {}).get("noteDetailMap")) or {}
        if len(note_map) == 1:
            note_id = next(iter(note_map.keys()))
    if not note_id:
        raise RuntimeError("无法解析小红书笔记 ID")
    return final, note_id, state


async def _download_video(
    client: httpx.AsyncClient, url: str, dest: Path
) -> None:
    headers = {
        "User-Agent": _PC_UA,
        "Referer": "https://www.xiaohongshu.com/",
        "Accept": "*/*",
    }
    cookie = _xhs_cookie()
    if cookie:
        headers["Cookie"] = cookie
    total = 0
    async with client.stream(
        "GET", url, headers=headers, follow_redirects=True, timeout=1800.0
    ) as resp:
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"视频下载失败（HTTP {resp.status_code}）")
        ct = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError("小红书返回了网页而非视频")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                total += len(chunk)
                if total > 500_000_000:
                    raise RuntimeError("视频超过大小限制")
                fh.write(chunk)
    if total < 50_000:
        raise RuntimeError("下载过小，链接可能已过期")


async def crawl_xhs_and_download(
    client: httpx.AsyncClient,
    share_text: str,
    dest: Path,
) -> dict[str, Any]:
    share_url = extract_xhs_share_url(share_text)
    if not share_url:
        urls = [u for u in extract_all_http_urls(share_text) if is_xhs_share_url(u)]
        share_url = urls[0] if urls else ""
    if not share_url:
        raise RuntimeError("文案中未找到小红书链接")

    final_url, note_id, state = await _fetch_note_page(client, share_url)
    meta = _meta_from_state(state, note_id)
    if meta.get("note_type") == "normal":
        raise RuntimeError("该笔记为图文，不含视频。请换一条视频笔记链接。")

    plays = _collect_video_urls(state)
    if not plays:
        raise RuntimeError("未能从小红书笔记解析出视频地址")

    last_err: Optional[Exception] = None
    for play_url in plays[:4]:
        try:
            await _download_video(client, play_url, dest)
            return {
                "aweme_id": note_id,
                "photo_id": note_id,
                "caption": meta.get("caption", ""),
                "duration": meta.get("duration", 0),
                "cover_url": meta.get("cover_url", ""),
                "play_url": play_url,
                "crawl_method": "xhs_page",
                "watermark_free": True,
                "local_path": dest,
                "source": "xhs_crawl",
            }
        except (RuntimeError, httpx.HTTPError) as exc:
            last_err = exc
            logger.info("小红书候选下载失败: %s", exc)
    raise RuntimeError(str(last_err or "小红书视频下载失败"))


async def try_crawl_xhs(
    client: httpx.AsyncClient, share_text: str, dest: Path
) -> Optional[dict[str, Any]]:
    if not (
        extract_xhs_share_url(share_text)
        or any(is_xhs_share_url(u) for u in extract_all_http_urls(share_text))
    ):
        return None
    try:
        return await crawl_xhs_and_download(client, share_text, dest)
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.info("小红书爬取失败: %s", exc)
        return None
