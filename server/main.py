import asyncio
import logging
import os
from contextlib import asynccontextmanager

from env_loader import load_project_env
from fq_koc_browser import (
    browser_sync_available,
    close_koc_browser,
    sync_koc_auth_via_browser,
)
from fq_koc_session import import_curl_text, load_koc_session, session_public_status

load_project_env()
load_koc_session()
import re
import secrets
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from hook_generator import FFMPEG

logging.basicConfig(level=logging.INFO)

from hongguo_api import fetch_episode_video_info, fetch_episodes
from ai_edit_planner import default_plan, plan_hook_edit, plan_to_dict
from hook_generator import generate_hook_video, render_keyword_splash_card
from douyin_material import (
    MATERIAL_DIR as DOUYIN_MATERIAL_DIR,
    find_best_material as find_douyin_best,
    fetch_douyin_body_source,
    resolve_share_url as resolve_douyin_share,
)
from material_common import is_douyin_url, is_kuaishou_url
from external_material import fetch_external_body_source
from kuaishou_material import (
    MATERIAL_DIR,
    find_best_material,
    resolve_share_url,
)
from fq_koc_material import MATERIAL_DIR as FQ_KOC_MATERIAL_DIR
from fq_koc_material import (
    cache_episode_from_url,
    clear_request_koc_options,
    config_status as fq_koc_config_status,
    fetch_fq_koc_episode,
    link_title_named_materials,
    material_help_messages,
    resolve_episode_download_url,
    save_episode_map,
    search_member_content,
    set_request_koc_options,
)
from qwen_client import config_info as llm_config_info
from qwen_client import is_configured as qwen_configured
from ai_edit_schemas import AiEditPlanRequest
from schemas import (
    FqKocCaptureRequest,
    FqKocCacheUrlRequest,
    FqKocMaterialRequest,
    FqKocSessionImportRequest,
    FqKocSessionSyncRequest,
    GenerateHookRequest,
    KuaishouMaterialRequest,
)

API_BASE = os.getenv(
    "HONGGUO_API_BASE",
    "http://nove.98tx.cn/api/index.php",
)
OFFICIAL_URL_BASE = os.getenv(
    "HONGGUO_OFFICIAL_URL",
    "https://www.novelquickapp.com/detail",
)
PAGE_SIZE = 10
HOT_TOP_LIMIT = 10
# 红果搜索 tab_type=12 为漫剧（motion comic），11 为真人短剧
HOT_TAB_TYPE = 12
HOT_SEED_QUERIES = (
    "漫剧",
    "AI漫剧",
    "热门",
    "玄幻",
    "修仙",
    "逆袭",
    "重生",
    "古风",
    "热血",
    "都市脑洞",
)
STATIC_DIR = Path(__file__).resolve().parent.parent / "public"
_hot_cache: dict[str, Any] = {"day": "", "items": []}
_hot_lock = asyncio.Lock()
DOWNLOAD_DIR = STATIC_DIR / "downloads"
TTS_CACHE_DIR = STATIC_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_TTL_SEC = 3600
GENERATE_JOBS: dict[str, dict[str, Any]] = {}
GENERATE_JOBS_LOCK = asyncio.Lock()

@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    yield
    if browser_sync_available():
        await close_koc_browser()


app = FastAPI(title="红果短剧检索", version="1.0.0", lifespan=_app_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _official_url(series_id: str) -> str:
    return f"{OFFICIAL_URL_BASE.rstrip('/')}?series_id={series_id}"


def _extract_dramas(
    payload: dict[str, Any], *, tab_type: int = 11
) -> list[dict[str, Any]]:
    dramas: list[dict[str, Any]] = []
    for tab in payload.get("search_tabs") or []:
        if tab.get("tab_type") != tab_type:
            continue
        for cell in tab.get("data") or []:
            for video in cell.get("video_data") or []:
                detail = video.get("video_detail") or {}
                intro = detail.get("series_intro") or video.get("abstract") or ""
                series_id = str(video.get("series_id") or "")
                dramas.append(
                    {
                        "id": series_id,
                        "url": _official_url(series_id) if series_id else "",
                        "title": (
                            detail.get("series_title")
                            or video.get("raw_book_name")
                            or video.get("title")
                            or ""
                        ),
                        "cover": video.get("cover")
                        or detail.get("series_cover")
                        or video.get("horiz_cover")
                        or "",
                        "intro": intro.strip(),
                        "sub_title": video.get("sub_title") or "",
                        "episodes": video.get("episode_cnt")
                        or detail.get("episode_cnt")
                        or 0,
                        "score": video.get("score") or "",
                        "play_count": int(
                            video.get("play_cnt")
                            or detail.get("series_play_cnt")
                            or 0
                        ),
                    }
                )
    return dramas


def _merge_dramas_by_play_count(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        series_id = str(item.get("id") or "")
        if not series_id:
            continue
        play_count = int(item.get("play_count") or 0)
        prev = by_id.get(series_id)
        if not prev or play_count > int(prev.get("play_count") or 0):
            by_id[series_id] = item
    return list(by_id.values())


async def _fetch_hot_top_from_upstream() -> list[dict[str, Any]]:
    """多关键词搜索漫剧 tab（tab_type=12），按播放量汇总今日热门。"""
    merged: list[dict[str, Any]] = []
    params_base = {
        "api": "search",
        "ts": "漫剧",
        "tab_type": str(HOT_TAB_TYPE),
        "offset": "0",
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        for keyword in HOT_SEED_QUERIES:
            try:
                resp = await client.get(
                    API_BASE, params={**params_base, "query": keyword}
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as exc:
                logging.warning("热门种子词「%s」请求失败: %s", keyword, exc)
                continue
            if data.get("success") is False:
                continue
            merged.extend(_extract_dramas(data, tab_type=HOT_TAB_TYPE))

    ranked = _merge_dramas_by_play_count(merged)
    ranked.sort(
        key=lambda x: int(x.get("play_count") or 0),
        reverse=True,
    )
    return ranked[:HOT_TOP_LIMIT]


async def get_today_hot_dramas(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    today = f"{date.today().isoformat()}:comic"
    async with _hot_lock:
        if (
            not force_refresh
            and _hot_cache.get("day") == today
            and _hot_cache.get("items")
        ):
            return list(_hot_cache["items"])

        items = await _fetch_hot_top_from_upstream()
        _hot_cache["day"] = today
        _hot_cache["items"] = items
        return list(items)


def _fuzzy_filter(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return items

    def score(item: dict[str, Any]) -> tuple[int, str]:
        title = (item.get("title") or "").lower()
        intro = (item.get("intro") or "").lower()
        if q in title:
            return (0, title)
        if all(c in title for c in q):
            return (1, title)
        if q in intro:
            return (2, title)
        return (3, title)

    matched = [i for i in items if score(i)[0] < 3]
    matched.sort(key=score)
    return matched if matched else items


async def _load_episodes(series_id: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        episodes = await fetch_episodes(client, series_id)
    if episodes:
        try:
            save_episode_map(series_id, episodes)
        except Exception as exc:
            logging.warning("保存剧集映射失败: %s", exc)
    return episodes


@app.get("/api/hot/today")
async def today_hot_dramas(
    refresh: bool = Query(False, description="强制刷新榜单缓存"),
):
    try:
        items = await get_today_hot_dramas(force_refresh=refresh)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"热门榜单获取失败: {exc}") from exc

    return {
        "date": date.today().isoformat(),
        "kind": "comic_drama",
        "kind_label": "漫剧",
        "count": len(items),
        "items": items,
    }


@app.get("/api/search")
async def search_dramas(
    q: str = Query(..., min_length=1, description="短剧名称关键词"),
    page: int = Query(1, ge=1),
):
    offset = (page - 1) * PAGE_SIZE
    params = {
        "api": "search",
        "ts": "短剧",
        "query": q.strip(),
        "tab_type": "11",
        "offset": str(offset),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(API_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"上游接口请求失败: {exc}") from exc

    if data.get("code") not in (0, None) and data.get("success") is False:
        raise HTTPException(
            status_code=502,
            detail=data.get("error") or data.get("message") or "上游返回错误",
        )

    items = _fuzzy_filter(_extract_dramas(data), q)
    return {
        "query": q,
        "page": page,
        "count": len(items),
        "items": items,
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "api_base": API_BASE,
        "ffmpeg": FFMPEG,
    }


@app.get("/api/config/status")
async def service_config_status(
    series_id: str = Query("", description="当前剧 series_id，用于检查本地素材"),
    item_id: str = Query("", description="分集 item_id"),
):
    """生成前可检查：Qwen / 达人中心 Cookie 是否已配置。"""
    env_file = load_project_env()
    fq = fq_koc_config_status()
    fq["hints"] = material_help_messages(series_id.strip(), item_id.strip())
    fq["session"] = session_public_status()
    fq["browser_sync"] = browser_sync_available()
    fq["browser_profile_ready"] = (
        FQ_KOC_MATERIAL_DIR / "pw_profile" / ".logged_in"
    ).is_file()
    return {
        "env_file_loaded": env_file is not None,
        "qwen": qwen_configured(),
        "llm": llm_config_info(),
        "fq_koc": fq,
    }


@app.post("/api/material/fq-koc/link-local")
async def link_fq_koc_local_aliases(
    series_id: str = Query(..., min_length=1, description="book_id / series_id"),
):
    """将「第N集.mp4」硬链为标准名 {series_id}_{item_id}.mp4（需先有剧集映射）。"""
    episodes = await _load_episodes(series_id)
    notes = link_title_named_materials(series_id)
    return {
        "ok": True,
        "series_id": series_id,
        "episode_count": len(episodes),
        "notes": notes,
    }


@app.get("/api/episodes")
async def series_episodes_query(
    series_id: str = Query(..., min_length=1, description="短剧 series_id"),
):
    """分集列表（query 参数，避免部分环境下 path 路由被静态资源拦截）。"""
    try:
        episodes = await _load_episodes(series_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"获取分集失败: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"series_id": series_id, "count": len(episodes), "episodes": episodes}


@app.get("/api/series/{series_id}/episodes")
async def series_episodes_path(series_id: str):
    try:
        episodes = await _load_episodes(series_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"获取分集失败: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"series_id": series_id, "count": len(episodes), "episodes": episodes}


def _schedule_file_cleanup(path: Path, background_tasks: BackgroundTasks) -> None:
    async def _delayed_remove() -> None:
        await asyncio.sleep(DOWNLOAD_TTL_SEC)
        path.unlink(missing_ok=True)

    background_tasks.add_task(_delayed_remove)


def _new_job_id() -> str:
    return secrets.token_urlsafe(9).replace("/", "_").replace("+", "-")


async def _set_job(job_id: str, **fields: Any) -> None:
    async with GENERATE_JOBS_LOCK:
        job = GENERATE_JOBS.setdefault(job_id, {})
        job.update(fields)


async def _run_generate_job(job_id: str, body: GenerateHookRequest) -> None:
    set_request_koc_options(
        create_url=body.fq_koc_create_url.strip(),
        cookie=body.fq_koc_cookie.strip(),
        ms_token=body.fq_koc_ms_token.strip(),
        a_bogus=body.fq_koc_a_bogus.strip(),
        download_body=body.fq_koc_download_body.strip(),
        direct_mp4_url=body.fq_koc_mp4_url.strip(),
    )
    try:
        ep_count = len(body.episode_item_ids)
        await _set_job(
            job_id,
            progress=f"正在生成（{ep_count} 集，含去重增强约需 {max(3, ep_count * 2)}–{ep_count * 4} 分钟）…",
        )
        async with httpx.AsyncClient(timeout=7200.0) as client:
            output_path, filename, gen_warning, edit_plan = await generate_hook_video(
                client,
                series_id=body.series_id,
                drama_title=body.drama_title or body.series_id,
                cover_url=body.cover_url.strip(),
                opening=body.opening.strip(),
                keyword=body.keyword.strip(),
                episode_item_ids=body.episode_item_ids,
                use_fq_koc_material=body.use_fq_koc_material,
                use_kuaishou_material=body.use_kuaishou_material,
                kuaishou_share_url=body.kuaishou_share_url.strip(),
                use_ai_edit=body.use_ai_edit,
                drama_intro=body.drama_intro.strip(),
                splash_title_font=body.splash_title_font,
                splash_subtitle_font=body.splash_subtitle_font,
                splash_badge=body.splash_badge.strip(),
            )
        # 保存成片（无 BackgroundTasks，直接写盘后更新 job）
        work_dir = output_path.parent
        video_bytes = output_path.read_bytes()
        shutil.rmtree(work_dir, ignore_errors=True)
        if len(video_bytes) < 100_000:
            raise RuntimeError("生成的视频过小，请重试")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        task_id = _new_job_id()
        saved = DOWNLOAD_DIR / f"{task_id}.mp4"
        saved.write_bytes(video_bytes)
        logging.getLogger(__name__).info(
            "成片已保存 %s（任务 job_id=%s，%d 字节，约 %.1f MB）",
            saved,
            job_id,
            len(video_bytes),
            len(video_bytes) / 1024 / 1024,
        )

        async def _delayed_remove() -> None:
            await asyncio.sleep(DOWNLOAD_TTL_SEC)
            saved.unlink(missing_ok=True)

        asyncio.create_task(_delayed_remove())

        await _set_job(
            job_id,
            status="completed",
            progress="生成完成",
            preview_url=f"/preview/{task_id}.mp4",
            download_url=f"/downloads/{task_id}.mp4",
            filename=filename,
            size=len(video_bytes),
            warning=gen_warning or "",
            edit_plan=edit_plan,
            config={
                "qwen": qwen_configured(),
                "fq_koc": fq_koc_config_status(),
            },
        )
    except Exception as exc:
        logging.exception("generate job %s failed", job_id)
        await _set_job(
            job_id,
            status="failed",
            progress="生成失败",
            error=str(exc),
        )
    finally:
        clear_request_koc_options()


def _hook_file_path(task_id: str) -> Path:
    if not re.fullmatch(r"[\w-]{8,64}", task_id):
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    path = DOWNLOAD_DIR / f"{task_id}.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已过期，请重新生成")
    return path


@app.get("/preview/{task_id}.mp4")
async def preview_hook_file(task_id: str):
    """页面内预览：inline 播放，不带 attachment。"""
    path = _hook_file_path(task_id)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={
            "Content-Disposition": "inline",
            "Accept-Ranges": "bytes",
        },
    )


@app.get("/downloads/{task_id}.mp4")
async def download_hook_file(task_id: str):
    """下载：带 attachment 触发保存。"""
    path = _hook_file_path(task_id)
    return FileResponse(
        path,
        media_type="video/mp4",
        filename="hook_video.mp4",
        headers={"Content-Disposition": 'attachment; filename="hook_video.mp4"'},
    )


@app.get("/api/material/fq-koc/search")
async def search_fq_koc_content(
    keyword: str = Query(..., min_length=1),
    tab_type: int = Query(6, description="6=漫剧等内容库 tab，与达人中心页一致"),
):
    """番茄达人中心内容库搜索（需 HONGGUO_FQ_KOC_COOKIE）。"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            items = await search_member_content(client, keyword, tab_type=tab_type)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"达人中心请求失败: {exc}") from exc
    return {"ok": True, "items": items}


@app.post("/api/fq-koc/session/import")
async def import_fq_koc_session(body: FqKocSessionImportRequest):
    """从 F12「Copy as cURL」导入并保存达人中心配置（写入 koc_session.json，可选回写 .env）。"""
    try:
        data = import_curl_text(body.curl_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "session": session_public_status(),
        "has_download_body": bool(
            data.get("download_body") or data.get("last_working_body")
        ),
    }


@app.get("/api/fq-koc/session")
async def get_fq_koc_session():
    return {
        "ok": True,
        "session": session_public_status(),
        "browser_sync": browser_sync_available(),
    }


@app.post("/api/fq-koc/session/sync")
async def sync_fq_koc_session(body: FqKocSessionSyncRequest):
    """
    用 Playwright 打开达人中心（或复用已登录配置），
    在页面内发起 batch_download 并自动保存 Cookie / msToken / a_bogus。
  """
    book_id = body.series_id.strip() or os.getenv(
        "HONGGUO_FQ_KOC_SYNC_BOOK_ID", ""
    ).strip()
    item_id = body.item_id.strip() or os.getenv(
        "HONGGUO_FQ_KOC_SYNC_ITEM_ID", ""
    ).strip()
    if not book_id or not item_id:
        raise HTTPException(
            status_code=400,
            detail="请提供 series_id 与 item_id（或 .env 配置 HONGGUO_FQ_KOC_SYNC_BOOK_ID / ITEM_ID）",
        )
    try:
        result = await sync_koc_auth_via_browser(
            book_id,
            item_id,
            open_browser=body.open_browser,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "session": session_public_status(),
        "sync": result,
    }


@app.get("/api/material/fq-koc/download-url")
async def get_fq_koc_download_url(
    series_id: str = Query(..., min_length=1),
    item_id: str = Query(..., min_length=1),
    try_browser: bool = Query(False, description="是否尝试 Playwright（需已安装）"),
):
    """自动解析单集明文 MP4 下载地址（本地缓存 / batch / 直链）。"""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            result = await resolve_episode_download_url(
                client,
                book_id=series_id,
                item_id=item_id,
                try_browser=try_browser,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": result.get("ok", False), **result}


@app.post("/api/material/fq-koc/capture")
async def capture_fq_koc_from_browser(body: FqKocCaptureRequest):
    """
    推广中心页面助手脚本回传：保存签名会话，并可选缓存 MP4。
    """
    from fq_koc_session import save_koc_session

    msg_parts: list[str] = []
    if body.create_url.strip() or body.cookie.strip():
        save_koc_session(
            create_url=body.create_url.strip(),
            cookie=body.cookie.strip(),
            download_body=body.download_body.strip(),
        )
        msg_parts.append("会话已更新")

    mp4 = (body.mp4_url or body.download_url or "").strip()
    series_id = body.series_id.strip()
    item_id = body.item_id.strip()

    if mp4.startswith("http") and series_id and item_id:
        async with httpx.AsyncClient(timeout=600.0) as client:
            path = await cache_episode_from_url(
                client,
                book_id=series_id,
                item_id=item_id,
                mp4_url=mp4,
            )
        rel = path.relative_to(STATIC_DIR)
        return {
            "ok": True,
            "message": f"已缓存第 {item_id} 集（{path.stat().st_size // (1024*1024)}MB）",
            "public_url": f"/{rel.as_posix()}",
            "cached": True,
        }

    if mp4.startswith("http"):
        return {
            "ok": True,
            "message": "已记录 MP4 地址（缺少 series_id/item_id，请在生成页选手动缓存）",
            "download_url": mp4,
        }

    return {
        "ok": bool(msg_parts),
        "message": "；".join(msg_parts) if msg_parts else "未收到 MP4 地址",
    }


@app.post("/api/material/fq-koc/cache-url")
async def cache_fq_koc_from_url(body: FqKocCacheUrlRequest):
    """从 CDN 直链下载单集到 public/materials/fq_koc/{series_id}_{item_id}.mp4。"""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            path = await cache_episode_from_url(
                client,
                book_id=body.series_id,
                item_id=body.item_id,
                mp4_url=body.mp4_url,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"下载失败: {exc}") from exc
    rel = path.relative_to(STATIC_DIR)
    return {
        "ok": True,
        "path": str(path),
        "public_url": f"/{rel.as_posix()}",
        "size": path.stat().st_size,
    }


@app.post("/api/material/fq-koc")
async def download_fq_koc_material(body: FqKocMaterialRequest):
    """通过达人中心 batch_download 下载单集明文 MP4。"""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            FQ_KOC_MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
            result = await fetch_fq_koc_episode(
                client,
                book_id=body.series_id,
                item_id=body.item_id,
                drama_title=body.drama_title or body.series_id,
                dest_dir=FQ_KOC_MATERIAL_DIR,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"达人中心请求失败: {exc}") from exc

    local = Path(result["local_path"])
    rel = local.relative_to(STATIC_DIR)
    return {
        "ok": True,
        "source": "fq_koc",
        "book_id": result.get("book_id"),
        "item_id": result.get("item_id"),
        "local_path": str(local),
        "public_url": f"/{rel.as_posix()}",
        "size": local.stat().st_size if local.is_file() else 0,
    }


@app.post("/api/material/kuaishou")
async def download_kuaishou_material(body: KuaishouMaterialRequest):
    """站外素材：先快手后抖音，保存到 public/materials/。"""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            preview: dict[str, Any] = {}
            share = body.share_url.strip()
            kw = body.keyword or body.drama_title
            if share and is_kuaishou_url(share):
                preview = await resolve_share_url(client, share)
            elif share and is_douyin_url(share):
                preview = await resolve_douyin_share(client, share)
            else:
                try:
                    preview = await find_best_material(
                        client, keyword=kw, drama_title=body.drama_title
                    )
                except Exception:
                    preview = await find_douyin_best(
                        client, keyword=kw, drama_title=body.drama_title
                    )
            MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
            result = await fetch_external_body_source(
                client,
                drama_title=body.drama_title or body.keyword or "external",
                keyword=body.keyword,
                share_url=body.share_url,
                dest_dir=MATERIAL_DIR.parent / "external",
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"站外素材请求失败: {exc}") from exc

    local = Path(result["local_path"])
    rel = local.relative_to(STATIC_DIR)
    return {
        "ok": True,
        "source": result.get("source", "kuaishou"),
        "photo_id": result.get("photo_id") or result.get("aweme_id"),
        "caption": result.get("caption"),
        "duration": result.get("duration"),
        "play_url": result.get("play_url"),
        "local_path": str(local),
        "public_url": f"/{rel.as_posix()}",
        "size": local.stat().st_size if local.is_file() else 0,
        "preview_match": preview,
    }


@app.post("/api/material/douyin")
async def download_douyin_material(body: KuaishouMaterialRequest):
    """仅抖音：搜索/解析并下载到 public/materials/douyin。"""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            if body.share_url.strip():
                preview = await resolve_douyin_share(client, body.share_url.strip())
            else:
                preview = await find_douyin_best(
                    client,
                    keyword=body.keyword or body.drama_title,
                    drama_title=body.drama_title,
                )
            DOUYIN_MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
            result = await fetch_douyin_body_source(
                client,
                drama_title=body.drama_title or body.keyword or "douyin",
                keyword=body.keyword,
                share_url=body.share_url,
                dest_dir=DOUYIN_MATERIAL_DIR,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"抖音请求失败: {exc}") from exc

    local = Path(result["local_path"])
    rel = local.relative_to(STATIC_DIR)
    return {
        "ok": True,
        "source": "douyin",
        "aweme_id": result.get("aweme_id"),
        "caption": result.get("caption"),
        "duration": result.get("duration"),
        "play_url": result.get("play_url"),
        "local_path": str(local),
        "public_url": f"/{rel.as_posix()}",
        "size": local.stat().st_size if local.is_file() else 0,
        "preview_match": preview,
    }


@app.get("/api/tts/search-hint.mp3")
async def tts_search_hint(
    title: str = Query(..., min_length=1, max_length=64, description="剧名"),
):
    """首页语音：请搜索《剧名》在红果短剧观看原片（Edge TTS，带缓存）。"""
    from edge_tts_narration import (
        edge_tts_available,
        ensure_search_hint_mp3,
        tts_enabled,
    )

    if not tts_enabled() or not edge_tts_available():
        raise HTTPException(status_code=503, detail="TTS 未启用或未安装 edge-tts")
    try:
        path = await ensure_search_hint_mp3(title.strip(), TTS_CACHE_DIR)
    except Exception as exc:
        logging.getLogger(__name__).warning("搜索提示 TTS 失败: %s", exc)
        raise HTTPException(status_code=500, detail="语音合成失败") from exc
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename="search-hint.mp3",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/generate/splash-preview.png")
async def splash_preview(
    keyword: str = Query(default="", description="片头关键词，显示为《关键词》"),
    title_font: Optional[int] = Query(default=None, ge=48, le=220),
    subtitle_font: Optional[int] = Query(default=None, ge=32, le=180),
    badge: str = Query(default="", max_length=24, description="封面下方文字，如 1-5"),
):
    """预览 1 秒片头标题卡（1920×1080 PNG）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "splash.png"
        render_keyword_splash_card(
            path,
            keyword.strip() or "短剧",
            title_font_px=title_font,
            subtitle_font_px=subtitle_font,
            badge_text=badge.strip(),
        )
        data = path.read_bytes()
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/generate/hook")
async def generate_hook(
    body: GenerateHookRequest,
    background_tasks: BackgroundTasks,
):
    """异步生成：立即返回 job_id，前端轮询 /api/generate/job/{id}。"""
    job_id = _new_job_id()
    await _set_job(job_id, status="running", progress="任务已创建，排队中…")
    background_tasks.add_task(_run_generate_job, job_id, body)
    return {"ok": True, "job_id": job_id, "status": "running"}


@app.get("/api/generate/job/{job_id}")
async def get_generate_job(job_id: str):
    if not re.fullmatch(r"[\w-]{8,64}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    async with GENERATE_JOBS_LOCK:
        job = GENERATE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"ok": True, "job_id": job_id, **job}


@app.post("/api/ai/edit-plan")
async def preview_ai_edit_plan(body: AiEditPlanRequest):
    """仅生成 AI 剪辑方案，不下载/合成视频。"""
    from hook_generator import _episode_duration_seconds

    labels: list[str] = []
    durations: list[float] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for index, item_id in enumerate(body.episode_item_ids, start=1):
            labels.append(body.episode_titles.get(item_id) or f"第{index}集")
            try:
                info = await fetch_episode_video_info(
                    client, item_id, book_id=body.series_id
                )
                durations.append(_episode_duration_seconds(info))
            except Exception:
                durations.append(120.0)
        plan = await plan_hook_edit(
            client,
            drama_title=body.drama_title or body.series_id,
            drama_intro=body.drama_intro,
            opening=body.opening.strip(),
            keyword=body.keyword.strip(),
            episode_labels=labels,
            episode_durations=durations,
        )
    return {"ok": True, "edit_plan": plan_to_dict(plan)}


# 静态资源：仅挂载 public 目录，且放在所有 API 路由之后
if STATIC_DIR.is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(STATIC_DIR)),
        name="assets",
    )

    @app.get("/")
    async def index_page():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/generate.html")
    async def generate_page():
        return FileResponse(STATIC_DIR / "generate.html")

    @app.get("/{filename}")
    async def public_file(filename: str):
        if filename.startswith("api") or "/" in filename or ".." in filename:
            raise HTTPException(status_code=404, detail="Not Found")
        path = STATIC_DIR / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path)
