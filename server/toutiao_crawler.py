"""今日头条分享爬取：短链 / 视频页 → m.toutiao.com info API → vod 直链。"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

import httpx

from douyin_crawler import _download_video, _mobile_headers, extract_all_http_urls
from material_common import is_http_url

logger = logging.getLogger(__name__)

_TOU_TIAO_HOSTS = (
    "m.toutiao.com",
    "www.toutiao.com",
    "toutiao.com",
    "www.toutiaocdn.com",
)

_TOU_TIAO_SHARE_URL_RE = re.compile(
    r"https?://(?:m\.|www\.)?toutiao\.com/[^\s\]\)\"'<>，。；;]+",
    re.IGNORECASE,
)

_SHORT_CODE_RE = re.compile(
    r"(?:m\.toutiao\.com/is/|toutiao\.com/is/)([A-Za-z0-9]+)",
    re.IGNORECASE,
)

_GROUP_ID_RE = re.compile(
    r"(?:/video/|/i|item/)(\d{8,})",
    re.IGNORECASE,
)


def is_toutiao_share_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _TOU_TIAO_HOSTS)


def extract_toutiao_share_url(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    m = _TOU_TIAO_SHARE_URL_RE.search(raw)
    if m:
        return m.group(0).rstrip("，。,.;；'\"")
    m2 = _SHORT_CODE_RE.search(raw)
    if m2:
        return f"https://m.toutiao.com/is/{m2.group(1)}/"
    return ""


def _group_id_from_url(url: str) -> str:
    m = _GROUP_ID_RE.search(url or "")
    return m.group(1) if m else ""


async def _resolve_share_url(client: httpx.AsyncClient, share_url: str) -> str:
    resp = await client.get(
        share_url.strip(),
        headers=_mobile_headers("https://www.toutiao.com/"),
        follow_redirects=True,
        timeout=30.0,
    )
    return str(resp.url)


async def _fetch_article_info(
    client: httpx.AsyncClient, group_id: str, referer: str
) -> dict[str, Any]:
    resp = await client.get(
        f"https://m.toutiao.com/i{group_id}/info/",
        headers=_mobile_headers(referer),
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    if not data:
        raise RuntimeError("头条接口未返回作品数据")
    return data


def _vod_params_from_token(token_b64: str) -> dict[str, str]:
    if not token_b64:
        raise RuntimeError("头条未返回播放令牌")
    try:
        token_json = json.loads(base64.b64decode(token_b64))
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("头条播放令牌解析失败") from exc
    inner = str(token_json.get("GetPlayInfoToken") or "")
    if not inner:
        raise RuntimeError("头条播放令牌无效")
    return {k: v[0] for k, v in parse_qs(inner).items() if v}


def _play_item_quality(item: dict[str, Any]) -> int:
    br = int(item.get("Bitrate") or 0)
    height = int(item.get("Height") or 0)
    width = int(item.get("Width") or 0)
    meta = item.get("VideoMeta") or {}
    if isinstance(meta, dict):
        height = max(height, int(meta.get("Height") or 0))
        width = max(width, int(meta.get("Width") or 0))
    definition = str(item.get("Definition") or "").lower()
    def_score = 0
    if "1080" in definition:
        def_score = 300_000
    elif "720" in definition:
        def_score = 200_000
    elif "540" in definition:
        def_score = 120_000
    return max(br, height * width, def_score)


def _pick_play_urls(vod_payload: dict[str, Any]) -> list[str]:
    result = vod_payload.get("Result") or {}
    data = result.get("Data") or {}
    items = data.get("PlayInfoList") or []
    if not isinstance(items, list) or not items:
        raise RuntimeError("头条未返回可播放地址")
    ranked = sorted(
        (x for x in items if isinstance(x, dict) and x.get("MainPlayUrl")),
        key=_play_item_quality,
        reverse=True,
    )
    if not ranked:
        raise RuntimeError("头条播放列表为空")
    urls: list[str] = []
    seen: set[str] = set()
    for item in ranked:
        main = str(item.get("MainPlayUrl") or "").strip()
        if is_http_url(main) and main not in seen:
            seen.add(main)
            urls.append(main)
        backup = item.get("BackupPlayUrl")
        if isinstance(backup, list):
            for u in backup:
                u = str(u).strip()
                if is_http_url(u) and u not in seen:
                    seen.add(u)
                    urls.append(u)
        elif is_http_url(str(backup or "")):
            u = str(backup).strip()
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls[:6]


async def _resolve_play_urls(
    client: httpx.AsyncClient, article: dict[str, Any], referer: str
) -> tuple[list[str], float, str]:
    token = str(article.get("play_auth_token_v2") or "")
    params = _vod_params_from_token(token)
    resp = await client.get(
        "https://vod.bytedanceapi.com/",
        params=params,
        headers=_mobile_headers(referer),
        timeout=30.0,
    )
    resp.raise_for_status()
    play_urls = _pick_play_urls(resp.json())
    duration = float(article.get("video_duration") or 0)
    cover = str(article.get("poster_url") or "")
    return play_urls, duration, cover


async def crawl_toutiao_and_download(
    client: httpx.AsyncClient,
    share_text: str,
    dest: Path,
) -> dict[str, Any]:
    share_url = extract_toutiao_share_url(share_text)
    if not share_url:
        urls = [u for u in extract_all_http_urls(share_text) if is_toutiao_share_url(u)]
        share_url = urls[0] if urls else ""
    if not share_url:
        raise RuntimeError("文案中未找到今日头条链接")

    final_url = await _resolve_share_url(client, share_url)
    group_id = _group_id_from_url(final_url) or _group_id_from_url(share_url)
    if not group_id:
        raise RuntimeError("无法解析头条作品 ID")

    article = await _fetch_article_info(client, group_id, final_url)
    play_urls, duration, cover = await _resolve_play_urls(client, article, final_url)

    last_err: Optional[Exception] = None
    meta: Optional[dict[str, Any]] = None
    for play_url in play_urls:
        try:
            meta = await _download_video(
                client,
                play_url,
                dest,
                referer=final_url,
                crawl_method="toutiao_vod_hd",
            )
            break
        except (RuntimeError, httpx.HTTPError) as exc:
            last_err = exc
            logger.info("头条候选下载失败: %s", exc)
    if not meta:
        raise RuntimeError(str(last_err or "头条视频下载失败"))

    meta.update(
        {
            "aweme_id": group_id,
            "duration": duration or meta.get("duration", 0),
            "cover_url": cover,
            "watermark_free": True,
            "source": "toutiao_crawl",
            "caption": "",
        }
    )
    return meta


async def try_crawl_toutiao(
    client: httpx.AsyncClient, share_text: str, dest: Path
) -> Optional[dict[str, Any]]:
    if not (extract_toutiao_share_url(share_text) or any(
        is_toutiao_share_url(u) for u in extract_all_http_urls(share_text)
    )):
        return None
    try:
        return await crawl_toutiao_and_download(client, share_text, dest)
    except (RuntimeError, httpx.HTTPError) as exc:
        logger.info("头条爬取失败: %s", exc)
        return None
