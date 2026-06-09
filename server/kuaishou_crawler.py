"""快手分享爬取：短链 / 作品页 → 页面 Apollo 数据 → MP4 直链。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from douyin_crawler import _download_video, _mobile_headers, extract_all_http_urls
from kuaishou_material import (
    _photo_id_from_share_url,
    _pick_play_url,
    _urls_from_manifest,
    download_material,
    get_video_by_photo_id,
    resolve_share_url,
)

logger = logging.getLogger(__name__)

_KS_HOSTS = (
    "kuaishou.com",
    "kuaishou.cn",
    "chenzhongtech.com",
)

_KS_SHARE_URL_RE = re.compile(
    r"https?://(?:v\.|www\.)?kuaishou\.com/[^\s\]\)\"'<>，。；;]+|"
    r"https?://[^\s\]\)\"'<>，。；;]*chenzhongtech\.com/[^\s\]\)\"'<>，。；;]+",
    re.IGNORECASE,
)

_PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_APOLLO_RE = re.compile(
    r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;",
    re.S,
)


def is_kuaishou_share_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _KS_HOSTS)


def extract_kuaishou_share_url(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    m = _KS_SHARE_URL_RE.search(raw)
    if m:
        return m.group(0).rstrip("，。,.;；'\"`")
    for u in extract_all_http_urls(raw):
        if is_kuaishou_share_url(u):
            return u
    return ""


def _score_mp4_url(url: str) -> int:
    u = url.lower()
    score = 0
    if ".mp4" in u:
        score += 50
    if "photo-video" in u:
        score += 40
    if "hd" in u:
        score += 20
    if "ndcimgs.com" in u or "kwaicdn" in u or "yximgs.com" in u:
        score += 10
    if "upic/" in u and "photo-video" not in u:
        score -= 30
    return score


def _urls_from_apollo(html: str) -> list[str]:
    m = _APOLLO_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key in ("photoUrl", "srcNoMark") and isinstance(val, str):
                    u = val.replace("\\u002F", "/").strip()
                    if u.startswith("http"):
                        found.append(u)
                if key == "manifest" and isinstance(val, str):
                    try:
                        man = json.loads(val)
                        found.extend(_urls_from_manifest(man))
                    except json.JSONDecodeError:
                        pass
                if key == "url" and isinstance(val, str):
                    u = val.replace("\\u002F", "/").strip()
                    if u.startswith("http") and (".mp4" in u or "photo-video" in u):
                        found.append(u)
                walk(val)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    out.sort(key=_score_mp4_url, reverse=True)
    return out


async def _resolve_page(
    client: httpx.AsyncClient, share_url: str
) -> tuple[str, str, list[str]]:
    resp = await client.get(
        share_url.strip(),
        headers={
            "User-Agent": _PC_UA,
            "Referer": "https://www.kuaishou.com/",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=30.0,
    )
    resp.raise_for_status()
    final = str(resp.url)
    html = resp.text or ""
    photo_id = _photo_id_from_share_url(final) or _photo_id_from_share_url(share_url) or ""
    plays = _urls_from_apollo(html)
    return final, photo_id, plays


async def _crawl_with_cookie(
    client: httpx.AsyncClient, share_url: str
) -> dict[str, Any]:
    meta = await resolve_share_url(client, share_url)
    return {
        **meta,
        "aweme_id": meta.get("photo_id", ""),
        "crawl_method": "kuaishou_api",
        "watermark_free": True,
        "caption": meta.get("caption", ""),
    }


async def crawl_kuaishou_and_download(
    client: httpx.AsyncClient,
    share_text: str,
    dest: Path,
) -> dict[str, Any]:
    share_url = extract_kuaishou_share_url(share_text)
    if not share_url:
        urls = [u for u in extract_all_http_urls(share_text) if is_kuaishou_share_url(u)]
        share_url = urls[0] if urls else ""
    if not share_url:
        raise RuntimeError("文案中未找到快手链接")

    final_url, photo_id, plays = await _resolve_page(client, share_url)
    play_url = plays[0] if plays else ""

    meta: dict[str, Any] = {
        "aweme_id": photo_id or dest.stem,
        "caption": "",
        "duration": 0.0,
        "cover_url": "",
        "crawl_method": "kuaishou_page",
        "watermark_free": True,
    }

    if not play_url:
        try:
            meta = await _crawl_with_cookie(client, share_url)
            play_url = str(meta.get("play_url") or "")
        except Exception as exc:
            logger.info("快手 Cookie API 失败: %s", exc)
            raise RuntimeError(
                "无法从快手页面解析视频。可配置 HONGGUO_KUAISHOU_COOKIE 后重试。"
            ) from exc

    if play_url:
        await download_material(client, play_url, dest)
        meta["play_url"] = play_url
        meta["aweme_id"] = photo_id or meta.get("photo_id") or dest.stem
        meta["local_path"] = dest
        meta["source"] = "kuaishou_crawl"
        return meta

    raise RuntimeError("快手未返回可下载地址")


async def try_crawl_kuaishou(
    client: httpx.AsyncClient, share_text: str, dest: Path
) -> Optional[dict[str, Any]]:
    if not (
        extract_kuaishou_share_url(share_text)
        or any(is_kuaishou_share_url(u) for u in extract_all_http_urls(share_text))
    ):
        return None
    try:
        return await crawl_kuaishou_and_download(client, share_text, dest)
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.info("快手爬取失败: %s", exc)
        return None
