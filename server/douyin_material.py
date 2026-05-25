"""抖音片源搜索、解析与下载（快手无结果时回退）。"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from material_common import (
    download_http_video,
    is_http_url,
    probe_decodes,
    score_caption,
    yt_dlp_download,
)

logger = logging.getLogger(__name__)

MATERIAL_DIR = Path(__file__).resolve().parent.parent / "public" / "materials" / "douyin"
SEARCH_URL = "https://www.douyin.com/aweme/v1/web/general/search/single/"
DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _cookie() -> str:
    return os.getenv("HONGGUO_DOUYIN_COOKIE", "").strip()


def _headers(referer: str = "https://www.douyin.com/") -> dict[str, str]:
    h = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
    }
    cookie = _cookie()
    if cookie:
        h["Cookie"] = cookie
    return h


def _play_url_from_aweme(aweme: dict[str, Any]) -> str:
    video = aweme.get("video") or {}
    for key in ("play_addr", "download_addr"):
        addr = video.get(key) or {}
        for u in addr.get("url_list") or []:
            if is_http_url(u):
                return str(u).strip()
    bit_rates = video.get("bit_rate") or []
    if isinstance(bit_rates, list) and bit_rates:
        best = bit_rates[-1] if bit_rates else {}
        if isinstance(best, dict):
            addr = best.get("play_addr") or {}
            for u in addr.get("url_list") or []:
                if is_http_url(u):
                    return str(u).strip()
    return ""


def _normalize_search_item(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    aweme = raw.get("aweme_info") or raw.get("aweme") or raw
    if not isinstance(aweme, dict):
        return None
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
    if not aweme_id:
        return None
    play_url = _play_url_from_aweme(aweme)
    stats = aweme.get("statistics") or {}
    digg = int(stats.get("digg_count") or stats.get("digg_count_str") or 0)
    duration_ms = int((aweme.get("video") or {}).get("duration") or 0)
    cover = ""
    cover_obj = (aweme.get("video") or {}).get("cover") or {}
    if isinstance(cover_obj, dict):
        urls = cover_obj.get("url_list") or []
        if urls:
            cover = str(urls[0])
    return {
        "aweme_id": aweme_id,
        "caption": str(aweme.get("desc") or ""),
        "play_url": play_url,
        "digg_count": digg,
        "duration": duration_ms / 1000.0 if duration_ms > 1000 else float(duration_ms),
        "cover_url": cover,
    }


async def search_videos(
    client: httpx.AsyncClient, keyword: str, *, limit: int = 15
) -> list[dict[str, Any]]:
    if not _cookie():
        raise RuntimeError(
            "未配置 HONGGUO_DOUYIN_COOKIE：请在浏览器登录 douyin.com 后复制 Cookie"
        )
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "search_channel": "aweme_general",
        "search_source": "normal_search",
        "keyword": keyword,
        "search_id": "",
        "query_correct_type": "1",
        "is_filter_search": "0",
        "offset": "0",
        "count": str(min(limit, 20)),
    }
    referer = f"https://www.douyin.com/search/{quote(keyword)}?type=video"
    resp = await client.get(
        SEARCH_URL,
        params=params,
        headers=_headers(referer),
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status_code") not in (0, None):
        msg = str(payload.get("status_msg") or payload.get("status_code"))
        raise RuntimeError(f"抖音搜索失败: {msg}")

    items: list[dict[str, Any]] = []
    data = payload.get("data") or []
    if isinstance(data, list):
        for block in data:
            if not isinstance(block, dict):
                continue
            item = _normalize_search_item(block)
            if item:
                items.append(item)
            if len(items) >= limit:
                break
    return items


async def get_video_by_aweme_id(
    client: httpx.AsyncClient, aweme_id: str
) -> dict[str, Any]:
    if not _cookie():
        raise RuntimeError("未配置 HONGGUO_DOUYIN_COOKIE")
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "aweme_id": aweme_id,
    }
    resp = await client.get(
        DETAIL_URL,
        params=params,
        headers=_headers(f"https://www.douyin.com/video/{aweme_id}"),
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    detail = (payload.get("aweme_detail") or payload.get("aweme_info") or {})
    item = _normalize_search_item({"aweme_info": detail})
    if not item:
        raise RuntimeError("抖音作品详情解析失败")
    if not item.get("play_url"):
        raise RuntimeError("抖音作品详情中无播放地址")
    return item


def _aweme_id_from_url(url: str) -> Optional[str]:
    patterns = [
        r"video/(\d+)",
        r"note/(\d+)",
        r"aweme_id=(\d+)",
        r"/(\d{15,})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


async def resolve_share_url(
    client: httpx.AsyncClient, share_url: str
) -> dict[str, Any]:
    share_url = share_url.strip()
    aweme_id = _aweme_id_from_url(share_url)
    if not aweme_id:
        try:
            resp = await client.get(
                share_url,
                follow_redirects=True,
                headers=_headers(),
                timeout=20.0,
            )
            aweme_id = _aweme_id_from_url(str(resp.url))
        except httpx.HTTPError as exc:
            logger.info("抖音短链跳转失败: %s", exc)

    if aweme_id:
        try:
            return await get_video_by_aweme_id(client, aweme_id)
        except Exception as exc:
            logger.info("抖音详情 API 失败，下载阶段将尝试 yt-dlp: %s", exc)
            return {
                "aweme_id": aweme_id,
                "caption": "",
                "play_url": "",
                "duration": 0.0,
            }

    raise RuntimeError(
        "无法从抖音链接解析作品 ID，请使用完整分享链接或配置 HONGGUO_DOUYIN_COOKIE"
    )


async def find_best_material(
    client: httpx.AsyncClient,
    *,
    keyword: str,
    drama_title: str = "",
) -> dict[str, Any]:
    queries: list[str] = []
    for q in (drama_title, keyword, "漫剧", "短剧"):
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    best: Optional[dict[str, Any]] = None
    best_score = -1
    for q in queries:
        try:
            items = await search_videos(client, q, limit=15)
        except Exception as exc:
            logger.warning("抖音搜索「%s」失败: %s", q, exc)
            continue
        for item in items:
            if not item.get("play_url") and not item.get("aweme_id"):
                continue
            score = score_caption(
                item.get("caption", ""), q, drama_title or keyword
            )
            score += min(int(item.get("digg_count") or 0) // 100_000, 5)
            if score > best_score:
                best_score = score
                best = {**item, "search_keyword": q, "match_score": score}
        if best and best_score >= 10:
            break

    if not best:
        raise RuntimeError(
            "抖音未找到可下载片源。请配置 HONGGUO_DOUYIN_COOKIE，"
            "或在抖音 App 搜索后复制分享链接传入。"
        )
    if not best.get("play_url") and best.get("aweme_id"):
        best = await get_video_by_aweme_id(client, str(best["aweme_id"]))
    return best


async def fetch_douyin_body_source(
    client: httpx.AsyncClient,
    *,
    drama_title: str,
    keyword: str = "",
    share_url: str = "",
    dest_dir: Path,
) -> dict[str, Any]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", drama_title)[:40] or "douyin"

    if share_url.strip():
        meta = await resolve_share_url(client, share_url.strip())
        dest = dest_dir / f"{safe}_{meta.get('aweme_id', 'share')}.mp4"
        play_url = str(meta.get("play_url") or "")
        if is_http_url(play_url) and ".mp4" in play_url.split("?")[0]:
            await download_http_video(
                client, play_url, dest, referer="https://www.douyin.com/"
            )
        else:
            yt_dlp_download(share_url.strip(), dest, cookie=_cookie())
    else:
        meta = await find_best_material(
            client, keyword=keyword or drama_title, drama_title=drama_title
        )
        play_url = str(meta["play_url"])
        dest = dest_dir / f"{safe}_{meta['aweme_id']}.mp4"
        await download_http_video(
            client, play_url, dest, referer="https://www.douyin.com/"
        )

    if not probe_decodes(dest):
        raise RuntimeError("抖音素材下载成功但无法解码，请换一条作品链接")

    return {
        "local_path": dest,
        "play_url": play_url,
        "aweme_id": str(meta.get("aweme_id") or ""),
        "caption": meta.get("caption", ""),
        "duration": float(meta.get("duration") or 0),
        "source": "douyin",
    }
