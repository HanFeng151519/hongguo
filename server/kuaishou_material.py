"""快手片源搜索、解析与下载（明文 MP4，用作推广素材）。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

GRAPHQL_DIR = Path(__file__).resolve().parent / "kuaishou_graphql"
GRAPHQL_URL = "https://www.kuaishou.com/graphql"
MATERIAL_DIR = Path(__file__).resolve().parent.parent / "public" / "materials" / "kuaishou"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Origin": "https://www.kuaishou.com",
    "Referer": "https://www.kuaishou.com/",
}


def _cookie_header() -> str:
    return os.getenv("HONGGUO_KUAISHOU_COOKIE", "").strip()


def _graphql_headers(referer: str = "") -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
    cookie = _cookie_header()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _load_query(name: str) -> str:
    path = GRAPHQL_DIR / name
    return path.read_text(encoding="utf-8")


async def _graphql(
    client: httpx.AsyncClient,
    *,
    operation: str,
    query: str,
    variables: dict[str, Any],
    referer: str = "",
) -> dict[str, Any]:
    resp = await client.post(
        GRAPHQL_URL,
        headers=_graphql_headers(referer),
        json={
            "operationName": operation,
            "query": query,
            "variables": variables,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        msg = json.dumps(payload["errors"], ensure_ascii=False)[:300]
        raise RuntimeError(f"快手 GraphQL 错误: {msg}")
    return payload.get("data") or {}


def _is_http_url(value: Any) -> bool:
    u = str(value or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def _urls_from_manifest(manifest: Any) -> list[str]:
    if not manifest:
        return []
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except json.JSONDecodeError:
            return []
    urls: list[tuple[int, str]] = []
    for adapt in manifest.get("adaptationSet") or []:
        if not isinstance(adapt, dict):
            continue
        for rep in adapt.get("representation") or []:
            if not isinstance(rep, dict):
                continue
            height = int(rep.get("height") or 0)
            for key in ("url", "backupUrl"):
                u = rep.get(key)
                if _is_http_url(u):
                    urls.append((height, str(u).strip()))
    urls.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _, u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_play_url(photo: dict[str, Any]) -> str:
    for u in _urls_from_manifest(photo.get("manifest")):
        return u
    if _is_http_url(photo.get("photoUrl")):
        return str(photo["photoUrl"]).strip()
    vr = photo.get("videoResource")
    if isinstance(vr, dict):
        for u in _urls_from_manifest(vr):
            return u
    return ""


def _score_caption(caption: str, keyword: str, drama_title: str) -> int:
    text = (caption or "").lower()
    score = 0
    for term in (keyword, drama_title):
        t = (term or "").strip().lower()
        if not t:
            continue
        if t in text:
            score += 10
        for part in re.split(r"[\s之·]+", t):
            if len(part) >= 2 and part in text:
                score += 3
    if "短剧" in text or "合集" in text or "第" in text:
        score += 2
    return score


async def search_videos(
    client: httpx.AsyncClient,
    keyword: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not _cookie_header():
        raise RuntimeError(
            "未配置 HONGGUO_KUAISHOU_COOKIE：请在浏览器登录 kuaishou.com 后复制 Cookie"
        )
    referer = f"https://www.kuaishou.com/search/video?searchKey={keyword}"
    data = await _graphql(
        client,
        operation="visionSearchPhoto",
        query=_load_query("search_query.graphql"),
        variables={
            "keyword": keyword,
            "pcursor": "",
            "page": "search",
            "searchSessionId": "",
            "webPageArea": "",
        },
        referer=referer,
    )
    block = data.get("visionSearchPhoto") or {}
    feeds = block.get("feeds") or []
    items: list[dict[str, Any]] = []
    for feed in feeds:
        photo = (feed or {}).get("photo") or {}
        photo_id = str(photo.get("id") or "")
        if not photo_id:
            continue
        play_url = _pick_play_url(photo)
        items.append(
            {
                "photo_id": photo_id,
                "caption": str(photo.get("caption") or ""),
                "duration": float(photo.get("duration") or 0),
                "cover_url": str(photo.get("coverUrl") or ""),
                "play_url": play_url,
            }
        )
        if len(items) >= limit:
            break
    return items


async def get_video_by_photo_id(
    client: httpx.AsyncClient, photo_id: str
) -> dict[str, Any]:
    if not _cookie_header():
        raise RuntimeError("未配置 HONGGUO_KUAISHOU_COOKIE")
    data = await _graphql(
        client,
        operation="visionVideoDetail",
        query=_load_query("video_detail.graphql"),
        variables={
            "photoId": photo_id,
            "type": "video",
            "page": "search",
            "webPageArea": "",
        },
        referer=f"https://www.kuaishou.com/short-video/{photo_id}",
    )
    photo = (data.get("visionVideoDetail") or {}).get("photo") or {}
    play_url = _pick_play_url(photo)
    if not play_url:
        raise RuntimeError("未能从快手作品详情解析出播放地址")
    return {
        "photo_id": str(photo.get("id") or photo_id),
        "caption": str(photo.get("caption") or ""),
        "duration": float(photo.get("duration") or 0),
        "cover_url": str(photo.get("coverUrl") or ""),
        "play_url": play_url,
    }


def _photo_id_from_share_url(share_url: str) -> Optional[str]:
    u = share_url.strip()
    if not u:
        return None
    # short-video/3xxxxx
    m = re.search(r"short-video/([A-Za-z0-9_-]+)", u)
    if m:
        return m.group(1)
    m = re.search(r"photoId=([A-Za-z0-9_-]+)", u)
    if m:
        return m.group(1)
    return None


async def resolve_share_url(
    client: httpx.AsyncClient, share_url: str
) -> dict[str, Any]:
    """解析 v.kuaishou.com / 作品页链接。"""
    share_url = share_url.strip()
    photo_id = _photo_id_from_share_url(share_url)
    if photo_id:
        return await get_video_by_photo_id(client, photo_id)

    # 短链跳转
    resp = await client.get(
        share_url,
        headers={
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Referer": "https://www.kuaishou.com/",
        },
        follow_redirects=True,
        timeout=30.0,
    )
    final = str(resp.url)
    photo_id = _photo_id_from_share_url(final)
    if photo_id:
        return await get_video_by_photo_id(client, photo_id)
    raise RuntimeError(f"无法从链接解析快手作品 ID: {final[:120]}")


async def find_best_material(
    client: httpx.AsyncClient,
    *,
    keyword: str,
    drama_title: str = "",
) -> dict[str, Any]:
    """按剧名/关键词在快手搜索，选最相关且可下载的一条。"""
    queries = []
    for q in (drama_title, keyword, "聚宝仙盆"):
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    best: Optional[dict[str, Any]] = None
    best_score = -1
    for q in queries:
        try:
            items = await search_videos(client, q, limit=15)
        except Exception as exc:
            logger.warning("快手搜索「%s」失败: %s", q, exc)
            continue
        for item in items:
            if not item.get("play_url"):
                continue
            score = _score_caption(
                item.get("caption", ""), q, drama_title or keyword
            )
            if score > best_score:
                best_score = score
                best = {**item, "search_keyword": q, "match_score": score}
        if best and best_score >= 10:
            break

    if not best:
        raise RuntimeError(
            "快手未找到可下载片源。请配置 HONGGUO_KUAISHOU_COOKIE，"
            "或在快手 App 搜索该剧后复制作品链接，用 kuaishou_share_url 传入。"
        )
    return best


async def download_material(
    client: httpx.AsyncClient,
    play_url: str,
    dest: Path,
    *,
    max_bytes: int = 500_000_000,
) -> Path:
    if not _is_http_url(play_url):
        raise RuntimeError("快手播放地址无效")
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Referer": "https://www.kuaishou.com/",
    }
    total = 0
    async with client.stream(
        "GET", play_url, headers=headers, timeout=1800.0, follow_redirects=True
    ) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("快手素材超过大小限制")
                fh.write(chunk)
    if total < 50_000:
        raise RuntimeError("快手素材下载过小，可能链接已过期")
    logger.info("快手素材已下载 %.1f MB -> %s", total / 1024 / 1024, dest)
    return dest


def _ffmpeg_path() -> str:
    from ffmpeg_util import resolve_ffmpeg_exe

    return resolve_ffmpeg_exe()


def probe_decodes(path: Path) -> bool:
    proc = subprocess.run(
        [_ffmpeg_path(), "-hide_banner", "-i", str(path), "-t", "2", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    nums = [int(x) for x in re.findall(r"frame=\s*(\d+)", proc.stderr or "")]
    return bool(nums) and nums[-1] >= 5


async def fetch_kuaishou_body_source(
    client: httpx.AsyncClient,
    *,
    drama_title: str,
    keyword: str = "",
    share_url: str = "",
    dest_dir: Path,
) -> dict[str, Any]:
    """
    解析并下载快手正片素材，返回可供 hook_generator 使用的本地路径与元信息。
    """
    if share_url.strip():
        meta = await resolve_share_url(client, share_url.strip())
    else:
        meta = await find_best_material(
            client, keyword=keyword or drama_title, drama_title=drama_title
        )

    play_url = str(meta["play_url"])
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", drama_title)[:40] or "kuaishou"
    dest = dest_dir / f"{safe_name}_{meta['photo_id']}.mp4"
    await download_material(client, play_url, dest)
    if not probe_decodes(dest):
        raise RuntimeError("快手素材下载成功但无法解码，请换一条作品链接")

    return {
        "local_path": dest,
        "play_url": play_url,
        "photo_id": meta["photo_id"],
        "caption": meta.get("caption", ""),
        "duration": float(meta.get("duration") or 0),
        "source": "kuaishou",
    }
