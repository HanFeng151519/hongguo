"""抖音分享爬取：优先无水印 play 直链 / download_addr，备选 playwm。"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx

from douyin_material import (
    DETAIL_URL,
    _aweme_id_from_url,
    _headers,
    _normalize_search_item,
    effective_douyin_cookie,
)
from material_common import is_http_url

logger = logging.getLogger(__name__)

_HTTP_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>，。；;]+", re.I)

_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

_CDN_HOST_HINTS = (
    "douyinvod",
    "zjcdn.com",
    "bytecdn",
    "bytevcloud",
    "amemv.com",
    "/video/tos/",
)

_PLAY_NOWM_RE = re.compile(
    r"https?://aweme\.snssdk\.com/aweme/v1/play(?!wm)/\?[^\"'\\s\]]*video_id=[^\"'\\s\]]+",
    re.I,
)
_PLAYWM_RE = re.compile(
    r"https?://aweme\.snssdk\.com/aweme/v1/playwm/\?[^\"'\\s\]]*video_id=[^\"'\\s\]]+",
    re.I,
)
_VIDEO_ID_RE = re.compile(r"video_id=(v[0-9a-zA-Z]+)")
_URI_RE = re.compile(r'"uri"\s*:\s*"(v[0-9][^"]+)"')

_RENDER_SCRIPT_RE = re.compile(
    r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>([^<]+)</script>',
    re.I,
)

# 页面 JSON 里 download_addr 多为无水印
_DOWNLOAD_ADDR_RE = re.compile(
    r'"download_addr"\s*:\s*\{[^}]*"url_list"\s*:\s*\[\s*"(https?://[^"]+)"',
    re.S,
)


def prefer_no_watermark() -> bool:
    return os.getenv("HONGGUO_DOUYIN_NO_WATERMARK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def prefer_crawl_quality() -> bool:
    """默认优先清晰度（可设 HONGGUO_CRAWL_PREFER_QUALITY=0 恢复旧策略）。"""
    return os.getenv("HONGGUO_CRAWL_PREFER_QUALITY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def crawl_only_default() -> bool:
    v = os.getenv("HONGGUO_DOUYIN_CRAWL_ONLY", "1").strip().lower()
    return v not in ("0", "false", "no")


def extract_all_http_urls(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for m in _HTTP_URL_RE.finditer(raw):
        u = m.group(0).rstrip("，。,.;；'\"")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _unescape_html(text: str) -> str:
    return (
        text.replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
    )


def is_share_page_url(url: str) -> bool:
    u = url.lower()
    return any(
        x in u
        for x in (
            "v.douyin.com",
            "douyin.com",
            "iesdouyin.com",
        )
    )


def _is_cdn_video_url(url: str) -> bool:
    u = url.lower()
    if not is_http_url(url):
        return False
    if "snssdk.com/aweme/v1/play" in u:
        return False
    if "douyinpic.com" in u:
        return False
    return any(h in u for h in _CDN_HOST_HINTS) or u.split("?")[0].endswith(".mp4")


def _play_api_url(vid: str, *, watermark: bool, ratio: str = "720p") -> str:
    vid = vid.strip()
    if not vid:
        return ""
    path = "playwm" if watermark else "play"
    return (
        f"https://aweme.snssdk.com/aweme/v1/{path}/"
        f"?video_id={vid}&ratio={ratio}&line=0"
    )


def _url_quality_score(url: str) -> int:
    u = (url or "").lower()
    score = 0
    for token, pts in (
        ("2160", 400),
        ("1080", 300),
        ("720", 200),
        ("540", 120),
        ("480", 80),
    ):
        if token in u:
            score = max(score, pts)
    if "ratio=1080" in u or "1080p" in u:
        score = max(score, 300)
    if "ratio=720" in u or "720p" in u:
        score = max(score, 200)
    m = re.search(r"br=(\d+)", u)
    if m:
        score = max(score, int(m.group(1)) // 8000)
    return score


def _bit_rate_urls_from_video(video: dict[str, Any]) -> list[tuple[str, int]]:
    ranked: list[tuple[str, int]] = []
    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        br = int(entry.get("bit_rate") or 0)
        gear = str(entry.get("gear_name") or "")
        gear_score = 0
        for token, pts in (("1080", 300), ("720", 200), ("540", 120)):
            if token in gear:
                gear_score = max(gear_score, pts)
        q = max(br // 1000, gear_score)
        addr = entry.get("play_addr") or {}
        if isinstance(addr, dict):
            for u in addr.get("url_list") or []:
                u = str(u).strip()
                if is_http_url(u):
                    ranked.append((u, q + _url_quality_score(u)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def _sort_candidates(urls: list[str]) -> list[str]:
    if not prefer_crawl_quality():
        return sorted(urls, key=_score_candidate)
    return sorted(
        urls,
        key=lambda u: (
            -_url_quality_score(u),
            1 if "playwm" in u.lower() else 0,
            _score_candidate(u),
        ),
    )


def _score_candidate(url: str) -> int:
    u = url.lower()
    if not prefer_no_watermark():
        if "playwm" in u and "video_id=" in u:
            return 10
        return 40
    if _is_cdn_video_url(url):
        return 0
    if "download_addr" in u or "/play/" in u and "playwm" not in u:
        return 2
    if "aweme/v1/play?" in u and "playwm" not in u and "video_id=" in u:
        return 3
    if "playwm" in u and "video_id=" in u:
        return 60
    if "playwm" in u:
        return 80
    if "snssdk.com" in u:
        return 50
    return 40


def _urls_from_json(obj: Any, out: list[str], *, prefer_download: bool) -> None:
    if isinstance(obj, dict):
        video = obj.get("video") if "video" in obj else None
        if isinstance(video, dict):
            if prefer_crawl_quality():
                for u, _ in _bit_rate_urls_from_video(video):
                    out.append(u)
            keys = ("download_addr", "play_addr", "play_addr_h264")
            if not prefer_download:
                keys = ("play_addr", "download_addr", "play_addr_h264")
            for key in keys:
                addr = video.get(key) or {}
                if isinstance(addr, dict):
                    for u in addr.get("url_list") or []:
                        if u:
                            out.append(str(u))
        for k, v in obj.items():
            if k != "video":
                _urls_from_json(v, out, prefer_download=prefer_download)
    elif isinstance(obj, list):
        for v in obj:
            _urls_from_json(v, out, prefer_download=prefer_download)


def _collect_candidates_from_html_full(html: str) -> list[str]:
    html = _unescape_html(html)
    found: list[str] = []
    seen: set[str] = set()
    no_wm = prefer_no_watermark()

    def add(u: str) -> None:
        u = u.strip().rstrip("\\")
        if not u or u in seen:
            return
        if not u.startswith("http"):
            return
        seen.add(u)
        found.append(u)

    for m in _DOWNLOAD_ADDR_RE.finditer(html):
        add(m.group(1))

    for m in _PLAY_NOWM_RE.finditer(html):
        add(m.group(0))

    for m in re.finditer(
        r"https?://[a-zA-Z0-9./_?=&%-]*(?:douyinvod|zjcdn\.com|bytevcloud)[a-zA-Z0-9./_?=&%-]*",
        html,
        re.I,
    ):
        add(m.group(0))

    for m in re.finditer(r'"url_list"\s*:\s*\[(.*?)\]', html, re.S):
        block = m.group(0)
        if no_wm and "download_addr" not in block and "playwm" in block:
            continue
        for u in re.findall(r'"(https?://[^"]+)"', m.group(1)):
            if "douyinpic.com" in u:
                continue
            if "playwm" in u and no_wm:
                continue
            if "playwm" in u or _is_cdn_video_url(u) or "aweme/v1/play" in u:
                add(u)

    ratios = ("1080p", "720p") if prefer_crawl_quality() else ("720p",)
    for m in _VIDEO_ID_RE.finditer(html):
        if no_wm:
            for ratio in ratios:
                add(_play_api_url(m.group(1), watermark=False, ratio=ratio))
        add(_play_api_url(m.group(1), watermark=True))

    for m in _URI_RE.finditer(html):
        if no_wm:
            for ratio in ratios:
                add(_play_api_url(m.group(1), watermark=False, ratio=ratio))
        add(_play_api_url(m.group(1), watermark=True))

    if not no_wm:
        for m in _PLAYWM_RE.finditer(html):
            add(m.group(0))

    m = _RENDER_SCRIPT_RE.search(html)
    if m:
        try:
            data = json.loads(urllib.parse.unquote(m.group(1)))
            extra: list[str] = []
            _urls_from_json(data, extra, prefer_download=no_wm)
            for u in extra:
                add(u)
        except (json.JSONDecodeError, ValueError):
            pass

    return _sort_candidates(found)


def _mobile_headers(referer: str = "https://www.douyin.com/") -> dict[str, str]:
    return {
        "User-Agent": _MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
    }


async def _fetch_share_html(
    client: httpx.AsyncClient, share_link: str
) -> tuple[str, str]:
    resp = await client.get(
        share_link.strip(),
        headers=_mobile_headers(),
        follow_redirects=True,
        timeout=30.0,
    )
    return resp.text or "", str(resp.url)


async def _verify_video_url(
    client: httpx.AsyncClient, url: str, *, referer: str
) -> bool:
    try:
        resp = await client.get(
            url,
            headers={**_mobile_headers(referer), "Range": "bytes=0-2047"},
            follow_redirects=True,
            timeout=25.0,
        )
        if resp.status_code not in (200, 206):
            return False
        ct = (resp.headers.get("content-type") or "").lower()
        if "video" in ct or "octet-stream" in ct:
            return True
        if len(resp.content) > 500:
            return True
        final = str(resp.url).lower()
        return any(h in final for h in _CDN_HOST_HINTS)
    except httpx.HTTPError:
        return False


def _crawl_method_label(url: str) -> str:
    u = url.lower()
    if "playwm" in u:
        return "playwm"
    if "aweme/v1/play" in u:
        return "play_nowm"
    if _is_cdn_video_url(url):
        return "cdn"
    return "direct"


async def _download_video(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    referer: str,
    crawl_method: str,
) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {**_mobile_headers(referer), "Accept": "*/*"}
    total = 0
    final_url = url
    async with client.stream(
        "GET",
        url,
        headers=headers,
        follow_redirects=True,
        timeout=1800.0,
    ) as resp:
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"视频地址返回 HTTP {resp.status_code}")
        final_url = str(resp.url)
        ct = (resp.headers.get("content-type") or "").lower()
        if "text/html" in ct:
            raise RuntimeError("链接返回网页而非视频")
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=2 * 1024 * 1024):
                total += len(chunk)
                if total > 500_000_000:
                    raise RuntimeError("视频超过大小限制")
                fh.write(chunk)
    if total < 50_000:
        raise RuntimeError("下载过小，链接可能已失效")

    aweme_id = _aweme_id_from_url(referer) or _aweme_id_from_url(final_url) or ""
    return {
        "aweme_id": aweme_id or dest.stem,
        "caption": "",
        "play_url": final_url,
        "duration": 0.0,
        "cover_url": "",
        "crawl_method": crawl_method,
        "watermark_free": "playwm" not in url.lower(),
        "local_path": dest,
        "source": "douyin_crawl",
    }


async def _resolve_from_share_link(
    client: httpx.AsyncClient, share_link: str
) -> tuple[list[str], str]:
    html, final_url = await _fetch_share_html(client, share_link)
    candidates = _collect_candidates_from_html_full(html)

    aweme_id = _aweme_id_from_url(final_url) or _aweme_id_from_url(share_link)
    if aweme_id and "iesdouyin.com/share/video" not in final_url:
        ies = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        try:
            html2, url2 = await _fetch_share_html(client, ies)
            for u in _collect_candidates_from_html_full(html2):
                if u not in candidates:
                    candidates.append(u)
            final_url = url2
        except httpx.HTTPError:
            pass

    candidates = _sort_candidates(candidates)
    return candidates, final_url


async def _try_share_urls(
    client: httpx.AsyncClient, share_urls: list[str], dest: Path
) -> Optional[dict[str, Any]]:
    for share in share_urls:
        try:
            plays, referer = await _resolve_from_share_link(client, share)
        except httpx.HTTPError:
            continue
        for play in plays:
            if not await _verify_video_url(client, play, referer=referer):
                logger.info("跳过无效候选 %s", play[:100])
                continue
            method = _crawl_method_label(play)
            try:
                return await _download_video(
                    client, play, dest, referer=referer, crawl_method=method
                )
            except (RuntimeError, httpx.HTTPError) as exc:
                logger.info("候选下载失败 %s", exc)
    return None


def _play_url_from_aweme_detail(aweme: dict[str, Any]) -> str:
    """API 详情：清晰度模式下优先 bit_rate 最高档。"""
    video = aweme.get("video") or {}
    if prefer_crawl_quality():
        for u, _ in _bit_rate_urls_from_video(video):
            return u
    keys = (
        ("download_addr", "play_addr")
        if prefer_no_watermark()
        else ("play_addr", "download_addr")
    )
    for key in keys:
        addr = video.get(key) or {}
        for u in addr.get("url_list") or []:
            u = str(u).strip()
            if u.startswith("http"):
                return u
    for u, _ in _bit_rate_urls_from_video(video):
        return u
    return ""


async def _crawl_with_cookie(
    client: httpx.AsyncClient, share_link: str
) -> dict[str, Any]:
    aweme_id = _aweme_id_from_url(share_link)
    if not aweme_id:
        _, final = await _fetch_share_html(client, share_link)
        aweme_id = _aweme_id_from_url(final)
    if not aweme_id:
        raise RuntimeError("无法解析作品 ID")

    resp = await client.get(
        DETAIL_URL,
        params={"device_platform": "webapp", "aid": "6383", "aweme_id": aweme_id},
        headers=_headers(f"https://www.douyin.com/video/{aweme_id}"),
        timeout=30.0,
    )
    payload = resp.json()
    detail = payload.get("aweme_detail") or payload.get("aweme_info") or {}
    play_url = _play_url_from_aweme_detail(detail)
    if not play_url:
        item = _normalize_search_item({"aweme_info": detail})
        if not item or not item.get("play_url"):
            raise RuntimeError("接口未返回播放地址")
        play_url = str(item["play_url"])
    item = _normalize_search_item({"aweme_info": detail}) or {}
    item["play_url"] = play_url
    item["crawl_method"] = "api_nowm" if prefer_no_watermark() else "api"
    item["watermark_free"] = "playwm" not in play_url.lower()
    item["caption"] = ""
    return item


async def crawl_and_download(
    client: httpx.AsyncClient,
    share_text: str,
    dest: Path,
) -> dict[str, Any]:
    from kuaishou_crawler import try_crawl_kuaishou
    from toutiao_crawler import try_crawl_toutiao
    from xhs_crawler import try_crawl_xhs

    kuaishou = await try_crawl_kuaishou(client, share_text, dest)
    if kuaishou:
        return kuaishou

    toutiao = await try_crawl_toutiao(client, share_text, dest)
    if toutiao:
        return toutiao

    xhs = await try_crawl_xhs(client, share_text, dest)
    if xhs:
        return xhs

    urls = extract_all_http_urls(share_text)
    if not urls:
        raise RuntimeError("文案中未找到 http 链接")

    share_urls = [u for u in urls if is_share_page_url(u)] or urls[:1]
    got = await _try_share_urls(client, share_urls, dest)
    if got:
        return got

    if effective_douyin_cookie():
        share = share_urls[0]
        meta = await _crawl_with_cookie(client, share)
        play_url = str(meta.get("play_url") or "")
        if is_http_url(play_url):
            from material_common import download_http_video

            await download_http_video(
                client, play_url, dest, referer="https://www.douyin.com/"
            )
            return {**meta, "local_path": dest, "source": "douyin_crawl"}

    raise RuntimeError(
        "无法获取视频。若需无水印，请确认链接有效或填写可选 Cookie 后重试。"
    )
