import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = os.getenv(
    "HONGGUO_API_BASE",
    "http://nove.98tx.cn/api/index.php",
)


async def _request(client: httpx.AsyncClient, **params: str) -> dict[str, Any]:
    query = {"ts": "短剧", **params}
    resp = await client.get(API_BASE, params=query)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") is False:
        raise RuntimeError(data.get("error") or "上游接口返回失败")
    if data.get("code") not in (0, None) and data.get("message") not in (
        "SUCCESS",
        None,
    ):
        if data.get("code") != 0:
            raise RuntimeError(data.get("message") or "上游接口返回错误")
    return data


async def fetch_episodes(client: httpx.AsyncClient, series_id: str) -> list[dict[str, Any]]:
    data = await _request(client, api="directory", book_id=series_id)
    lists = (data.get("data") or {}).get("lists") or []
    episodes = []
    for item in lists:
        episodes.append(
            {
                "title": item.get("title") or "",
                "item_id": str(item.get("item_id") or ""),
                "version": item.get("version") or "",
            }
        )
    return [e for e in episodes if e["item_id"]]


def _is_http_url(value: Any) -> bool:
    u = str(value or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def _play_url_from_track(track: dict[str, Any]) -> str:
    """只接受可直链下载的 http(s) 地址；fplay 的 main_url 常为加密 blob。"""
    for key in ("main_url", "backup_url_1", "backup_url_2"):
        candidate = track.get(key)
        if _is_http_url(candidate):
            return str(candidate).strip()
    return ""


def _merge_fplay_meta(base: dict[str, Any], fplay: dict[str, Any]) -> dict[str, Any]:
    """合并 fplay 的 DRM 字段，不覆盖已有 http 播放地址。"""
    merged = {**base}
    skip = frozenset({"main_url", "backup_url_1", "backup_url_2"})
    for key, value in fplay.items():
        if key in skip:
            continue
        if value is not None and value != "" and not merged.get(key):
            merged[key] = value
    return merged


def _pick_video_track(video_model: dict[str, Any]) -> dict[str, Any]:
    video_list = video_model.get("video_list") or {}
    by_def: dict[str, dict[str, Any]] = {}
    for item in video_list.values():
        if isinstance(item, dict) and _play_url_from_track(item):
            by_def[str(item.get("definition") or "")] = item

    for definition in ("1080p", "720p", "540p", "480p", "360p", "high", "normal"):
        if definition in by_def:
            return by_def[definition]

    for item in video_list.values():
        if isinstance(item, dict) and _play_url_from_track(item):
            return item
    raise RuntimeError("未找到可播放的视频地址")


def _pick_play_url(video_model: dict[str, Any]) -> str:
    track = _pick_video_track(video_model)
    url = _play_url_from_track(track)
    if url:
        return url
    raise RuntimeError("未找到可播放的视频地址")


def _episode_video_meta(
    video_model: dict[str, Any], track: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    thumbs = video_model.get("big_thumbs") or []
    auth = str(payload.get("authorization") or "").strip()
    if not auth:
        auth = os.getenv("HONGGUO_AUTHORIZATION", "").strip()
    return {
        "encrypt": bool(track.get("encrypt")),
        "encryption_method": str(track.get("encryption_method") or ""),
        "key_seed": str(video_model.get("key_seed") or ""),
        "spade_a": str(track.get("spade_a") or ""),
        "kid": str(track.get("kid") or ""),
        "video_id": str(video_model.get("video_id") or ""),
        "file_id": str(track.get("file_id") or ""),
        "definition": str(track.get("definition") or ""),
        "authorization": auth,
        "big_thumbs": thumbs[0] if thumbs else {},
    }


async def _fetch_fplay_tracks(
    client: httpx.AsyncClient, fallback_api: str
) -> dict[str, dict[str, Any]]:
    """通过 fplay 拉取全部清晰度，优先找 1080p / 未加密。"""
    if not fallback_api:
        return {}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Referer": "https://reading.snssdk.com/",
    }
    try:
        resp = await client.get(fallback_api, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("fplay 拉流失败: %s", exc)
        return {}

    video_list = (
        (data.get("video_info") or {}).get("data") or {}
    ).get("video_list") or {}
    by_def: dict[str, dict[str, Any]] = {}
    for item in video_list.values():
        if not isinstance(item, dict):
            continue
        if _play_url_from_track(item) or item.get("spade_a") or item.get("kid"):
            by_def[str(item.get("definition") or "")] = item
    return by_def


async def fetch_episode_video_info(
    client: httpx.AsyncClient,
    item_id: str,
    *,
    book_id: str = "",
) -> dict[str, Any]:
    params: dict[str, str] = {
        "api": "video",
        "video_id": item_id,
        "item_id": item_id,
    }
    if book_id:
        params["book_id"] = book_id
    data = await _request(client, **params)
    payload = (data.get("data") or {}).get("data") or data.get("data") or {}
    video_model = payload.get("video_model") or {}

    track = _pick_video_track(video_model)
    url = _play_url_from_track(track) or _pick_play_url(video_model)

    fplay_tracks = await _fetch_fplay_tracks(
        client, str(video_model.get("fallback_api") or "")
    )
    if fplay_tracks:
        for definition in ("1080p", "720p", "540p", "480p"):
            if definition not in fplay_tracks:
                continue
            fplay_track = fplay_tracks[definition]
            fplay_url = _play_url_from_track(fplay_track)
            if fplay_url:
                track = fplay_track
                url = fplay_url
            else:
                track = _merge_fplay_meta(track, fplay_track)
            break

    if not _is_http_url(url):
        raise RuntimeError(
            "未找到可下载的视频地址：上游 main_url 为加密 blob，非 http 链接"
        )
    duration = float(video_model.get("video_duration") or 120)
    meta = _episode_video_meta(video_model, track, payload)
    return {
        "url": url,
        "duration": duration,
        **meta,
    }


async def fetch_episode_play_url(
    client: httpx.AsyncClient,
    item_id: str,
    *,
    book_id: str = "",
) -> tuple[str, float]:
    info = await fetch_episode_video_info(client, item_id, book_id=book_id)
    return str(info["url"]), float(info["duration"])
