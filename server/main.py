import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager

# Windows：uvicorn 下 Playwright 需 Proactor 事件循环；macOS/Linux 不改动默认策略
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from env_loader import load_project_env
from fq_koc_browser import (
    browser_sync_available,
    close_koc_browser,
    ensure_koc_login,
    sync_koc_auth_via_browser,
)
from fq_koc_session import import_curl_text, load_koc_session, session_public_status

load_project_env()
from ffmpeg_util import configure_ffmpeg_env

configure_ffmpeg_env()
load_koc_session()
import re
import secrets
import shutil
import socket
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from hook_generator import FFMPEG

logging.basicConfig(level=logging.INFO)

from hongguo_api import (
    duration_from_episode_info,
    fetch_episode_video_info,
    fetch_episodes,
)
from ai_edit_planner import default_plan, plan_hook_edit, plan_to_dict
from hook_generator import generate_hook_video, render_keyword_splash_card
from douyin_material import (
    MATERIAL_DIR as DOUYIN_MATERIAL_DIR,
    douyin_cookie_scope,
    effective_douyin_cookie,
    find_best_material as find_douyin_best,
    fetch_douyin_body_source,
    resolve_share_url as resolve_douyin_share,
)
from material_common import (
    extract_douyin_share_url,
    extract_kuaishou_share_url,
    extract_toutiao_share_url,
    extract_xhs_share_url,
    is_douyin_url,
    is_kuaishou_url,
)
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
from ai_edit_schemas import AiEditPlanRequest, ManualEditPlanDraftRequest
from schemas import (
    FqKocCaptureRequest,
    FqKocCacheUrlRequest,
    FqKocLoginRequest,
    FqKocMaterialRequest,
    FqKocSessionImportRequest,
    FqKocSessionSyncRequest,
    GenerateHookRequest,
    KuaishouMaterialRequest,
    DouyinCacheRequest,
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
HOT_TAB_TYPES = (11, 12)
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
WM_UPLOAD_DIR = STATIC_DIR / "uploads" / "watermark"
DOUYIN_CACHE_DIR = STATIC_DIR / "cache" / "douyin"
WM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
WM_JOBS_DIR = WM_UPLOAD_DIR / "jobs"
WM_JOBS_DIR.mkdir(parents=True, exist_ok=True)
WM_MAX_BYTES = 500 * 1024 * 1024
WM_JOBS: dict[str, dict[str, Any]] = {}
WM_JOBS_LOCK = asyncio.Lock()


def _wm_job_file(job_id: str) -> Path:
    safe = re.sub(r"[^\w\-]", "", job_id) or "job"
    return WM_JOBS_DIR / f"{safe}.json"


def _load_wm_job(job_id: str) -> dict[str, Any] | None:
    path = _wm_job_file(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _save_wm_job(job_id: str, job: dict[str, Any]) -> None:
    path = _wm_job_file(job_id)
    try:
        payload = {**job, "job_id": job_id}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
TTS_CACHE_DIR = STATIC_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_TTL_SEC = 3600
GENERATE_JOBS: dict[str, dict[str, Any]] = {}
GENERATE_JOBS_LOCK = asyncio.Lock()

@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    if sys.platform == "win32":
        lan = _lan_ipv4()
        if lan:
            port = int(os.environ.get("PORT", "8000"))
            logging.getLogger("uvicorn.error").warning(
                "【手机访问】http://%s:%s — 若手机打不开，请双击 scripts\\allow-firewall-8000.bat 并点「是」放行防火墙",
                lan,
                port,
            )
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
    """多关键词搜索短剧+漫剧（tab_type=11/12），按播放量汇总今日热门。"""
    merged: list[dict[str, Any]] = []
    base_by_tab = {
        11: {"api": "search", "ts": "短剧", "tab_type": "11", "offset": "0"},
        12: {"api": "search", "ts": "漫剧", "tab_type": "12", "offset": "0"},
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        for tab_type in HOT_TAB_TYPES:
            params_base = base_by_tab.get(tab_type, {})
            if not params_base:
                continue
            for keyword in HOT_SEED_QUERIES:
                try:
                    resp = await client.get(
                        API_BASE, params={**params_base, "query": keyword}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except httpx.HTTPError as exc:
                    logging.warning(
                        "热门种子词「%s」请求失败(tab=%s): %s",
                        keyword,
                        tab_type,
                        exc,
                    )
                    continue
                if data.get("success") is False:
                    continue
                merged.extend(_extract_dramas(data, tab_type=tab_type))

    ranked = _merge_dramas_by_play_count(merged)
    ranked.sort(
        key=lambda x: int(x.get("play_count") or 0),
        reverse=True,
    )
    return ranked[:HOT_TOP_LIMIT]


async def get_today_hot_dramas(*, force_refresh: bool = False) -> list[dict[str, Any]]:
    today = f"{date.today().isoformat()}:mixed"
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
        "kind": "mixed_drama",
        "kind_label": "短剧+漫剧",
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


def _lan_ipv4() -> Optional[str]:
    """本机局域网 IPv4（供手机浏览器访问）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _ascii_download_name(filename: str, fallback: str = "download") -> str:
    safe = Path(filename).name
    ascii_only = "".join(
        c for c in safe if ord(c) < 128 and c not in ('"', "\\", "\r", "\n")
    )
    if not ascii_only.strip(". "):
        suffix = Path(safe).suffix
        return f"{fallback}{suffix}" if suffix else fallback
    return ascii_only


def _content_disposition(disposition: str, filename: str, fallback: str = "download") -> str:
    """HTTP 头仅支持 latin-1；中文文件名用 filename* (RFC 5987)。"""
    from urllib.parse import quote

    safe = Path(filename).name
    ascii_name = _ascii_download_name(safe, fallback=fallback)
    if safe == ascii_name:
        return f'{disposition}; filename="{ascii_name}"'
    encoded = quote(safe, safe="")
    return f'{disposition}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "api_base": API_BASE,
        "ffmpeg": FFMPEG,
    }


@app.get("/api/lan-access")
async def lan_access(request: Request):
    """返回手机在同一 WiFi 下应使用的访问地址。"""
    host = request.url.hostname or "localhost"
    port = request.url.port or 8000
    lan_ip = _lan_ipv4()
    lan_url = f"http://{lan_ip}:{port}" if lan_ip else None
    return {
        "lan_ip": lan_ip,
        "lan_url": lan_url,
        "port": port,
        "local_url": f"http://127.0.0.1:{port}",
        "hint": (
            "手机与电脑连同一 WiFi，在浏览器打开 lan_url。"
            "若打不开，请在 Windows 以管理员运行 scripts/allow-firewall-8000.ps1 放行 8000 端口。"
        ),
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


async def _job_interrupt_requested(job_id: str) -> bool:
    async with GENERATE_JOBS_LOCK:
        job = GENERATE_JOBS.get(job_id) or {}
        return bool(job.get("interrupt_for_edit"))


async def _run_generate_job(job_id: str, body: GenerateHookRequest) -> None:
    set_request_koc_options(
        create_url=body.fq_koc_create_url.strip(),
        cookie=body.fq_koc_cookie.strip(),
        ms_token=body.fq_koc_ms_token.strip(),
        a_bogus=body.fq_koc_a_bogus.strip(),
        download_body=body.fq_koc_download_body.strip(),
        direct_mp4_url=body.fq_koc_mp4_url.strip(),
    )
    prev_session_mutable = os.getenv("HONGGUO_FQ_KOC_SESSION_MUTABLE", "")
    os.environ["HONGGUO_FQ_KOC_SESSION_MUTABLE"] = "0"
    try:
        ep_count = len(body.episode_item_ids)
        book_id = body.series_id.strip()
        all_episodes_local = False
        missing_count = ep_count
        if body.use_fq_koc_material and book_id:
            from fq_koc_material import episodes_missing_local

            missing_ids = episodes_missing_local(book_id, body.episode_item_ids)
            missing_count = len(missing_ids)
            all_episodes_local = missing_count == 0
            if all_episodes_local:
                logging.getLogger(__name__).info(
                    "job %s: 所选 %d 集均已本地缓存，跳过达人中心登录与下载",
                    job_id,
                    ep_count,
                )
            else:
                from fq_koc_browser import _auto_sync_enabled

                if _auto_sync_enabled() and browser_sync_available():
                    await _set_job(
                        job_id,
                        progress=(
                            "正在确认达人中心登录（全程仅一次）：如弹出浏览器请先完成登录"
                            "，已登录将自动跳过，请勿关闭浏览器…"
                        ),
                    )
                    try:
                        await ensure_koc_login(book_id)
                    except Exception as exc:
                        await _set_job(
                            job_id,
                            status="failed",
                            progress="登录失败",
                            error=str(exc),
                        )
                        return
        manual_edit_flow = bool(body.pause_for_manual_edit) and body.use_fq_koc_material
        if all_episodes_local and manual_edit_flow:
            dl_progress = f"所选 {ep_count} 集均已本地缓存，正在载入时间轴…"
        elif all_episodes_local:
            dl_progress = f"所选 {ep_count} 集均已本地缓存，正在生成成片…"
        elif manual_edit_flow:
            dl_progress = (
                f"正在下载缺失正片（{missing_count} 集，已跳过 {ep_count - missing_count} 集本地缓存）"
                "，完成后将自动进入时间轴编辑…"
            )
        else:
            dl_progress = (
                f"正在生成（{ep_count} 集，含去重增强约需 {max(3, ep_count * 2)}–{ep_count * 4} 分钟）…"
            )
        await _set_job(
            job_id,
            progress=dl_progress,
            phase="starting",
            manual_edit_flow=manual_edit_flow,
            episode_item_ids=list(body.episode_item_ids),
        )

        async def _on_materials_ready() -> None:
            await _set_job(
                job_id,
                phase="materials_ready",
                progress="正片已下载到本地，正在载入时间轴草稿…",
            )

        async def _should_interrupt() -> bool:
            return await _job_interrupt_requested(job_id)

        async with httpx.AsyncClient(timeout=7200.0) as client:
            try:
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
                    edit_plan_override=body.edit_plan,
                    job_id=job_id,
                    should_interrupt=_should_interrupt if manual_edit_flow else None,
                    on_materials_ready=_on_materials_ready if manual_edit_flow else None,
                )
            except Exception as exc:
                from generate_job_control import JobInterruptedForEdit

                if isinstance(exc, JobInterruptedForEdit):
                    await _set_job(
                        job_id,
                        status="awaiting_manual_edit",
                        phase="awaiting_manual_edit",
                        progress="已进入手动剪辑。请在下方时间轴调整片段，完成后点「继续生成成片」。",
                        edit_plan=exc.edit_plan,
                    )
                    return
                raise
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

        paths_to_remove = [saved]

        async def _delayed_remove() -> None:
            await asyncio.sleep(DOWNLOAD_TTL_SEC)
            for p in paths_to_remove:
                p.unlink(missing_ok=True)

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
        if prev_session_mutable:
            os.environ["HONGGUO_FQ_KOC_SESSION_MUTABLE"] = prev_session_mutable
        else:
            os.environ.pop("HONGGUO_FQ_KOC_SESSION_MUTABLE", None)
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
            drama_title=body.drama_title.strip(),
            open_browser=body.open_browser,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "session": session_public_status(),
        "sync": result,
    }


@app.post("/api/fq-koc/session/login-only")
async def login_only_fq_koc_session(body: FqKocLoginRequest):
    """
    仅打开浏览器等待用户完成达人中心登录，不触发下载动作。
    """
    book_id = body.series_id.strip() or os.getenv(
        "HONGGUO_FQ_KOC_SYNC_BOOK_ID", ""
    ).strip()
    if not book_id:
        raise HTTPException(
            status_code=400,
            detail="请先选择剧集，或在 .env 配置 HONGGUO_FQ_KOC_SYNC_BOOK_ID",
        )
    try:
        result = await ensure_koc_login(book_id, timeout_sec=body.timeout_sec)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "已登录",
        "session": session_public_status(),
        "sync": result,
    }


@app.get("/api/material/fq-koc/local-status")
async def get_fq_koc_local_status(
    series_id: str = Query(..., min_length=1),
    item_ids: str = Query(..., min_length=1, description="逗号分隔的 item_id"),
):
    """批量查询哪些分集已有可解码的本地正片（不触发达人中心下载）。"""
    from fq_koc_material import has_usable_local_episode

    ids = [x.strip() for x in item_ids.split(",") if x.strip()]
    statuses = {iid: has_usable_local_episode(series_id, iid) for iid in ids}
    cached = sum(1 for v in statuses.values() if v)
    return {
        "ok": True,
        "series_id": series_id,
        "episodes": statuses,
        "cached_count": cached,
        "total": len(ids),
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
        # 前端按业务文案处理，不抛 4xx 打断自动兜底。
        return {"ok": False, "source": "need_capture", "message": str(exc)}
    except httpx.HTTPError as exc:
        return {"ok": False, "source": "need_capture", "message": f"网络请求失败: {exc}"}
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "source": "need_capture",
            "message": "解析下载地址超时，已自动跳过并继续后续兜底流程",
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": "need_capture",
            "message": f"解析下载地址异常: {exc}",
        }
    return {"ok": result.get("ok", False), **result}


@app.get("/api/material/fq-koc/playback")
async def get_fq_koc_playback_url(
    series_id: str = Query(..., min_length=1),
    item_id: str = Query(..., min_length=1),
):
    """
    时间轴预览专用：本地走 /materials 静态路径；远端 CDN 走同源代理（避免 CORS 导致无法播放）。
    """
    from fq_koc_material import find_local_material, local_material_decode_ok

    path = find_local_material(series_id, item_id)
    if path and local_material_decode_ok(path):
        rel = path.relative_to(STATIC_DIR)
        return {
            "ok": True,
            "source": "local",
            "cached": True,
            "playback_url": f"/{rel.as_posix()}",
            "size": path.stat().st_size,
        }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            result = await resolve_episode_download_url(
                client,
                book_id=series_id,
                item_id=item_id,
                try_browser=False,
            )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    if not result.get("ok"):
        return {
            "ok": False,
            "source": result.get("source", "need_capture"),
            "message": result.get("message", "未找到可播放素材"),
        }

    dl = str(result.get("download_url") or "").strip()
    if dl.startswith("/"):
        return {
            "ok": True,
            "playback_url": dl,
            "proxied": False,
            **{k: v for k, v in result.items() if k != "download_url"},
        }

    if dl.startswith("http"):
        return {
            "ok": True,
            "source": result.get("source", "remote"),
            "playback_url": (
                f"/api/material/fq-koc/stream"
                f"?series_id={series_id}&item_id={item_id}"
            ),
            "proxied": True,
            "remote_url": dl[:120],
        }

    return {"ok": False, "message": "未解析到有效 MP4 地址"}


@app.get("/api/material/fq-koc/stream")
async def stream_fq_koc_material(
    request: Request,
    series_id: str = Query(..., min_length=1),
    item_id: str = Query(..., min_length=1),
):
    """同源流式播放：本地 FileResponse（支持 Range）；远端经后端转发（避免浏览器 CORS）。"""
    from fq_koc_material import (
        DEFAULT_UA,
        find_local_material,
        local_material_decode_ok,
    )

    path = find_local_material(series_id, item_id)
    if path and local_material_decode_ok(path):
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=path.name,
        )

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0)) as client:
        result = await resolve_episode_download_url(
            client,
            book_id=series_id,
            item_id=item_id,
            try_browser=False,
        )
    if not result.get("ok"):
        raise HTTPException(
            status_code=404,
            detail=result.get("message", "未找到素材"),
        )

    dl = str(result.get("download_url") or "").strip()
    if dl.startswith("/"):
        local = STATIC_DIR / dl.lstrip("/")
        if local.is_file():
            return FileResponse(local, media_type="video/mp4", filename=local.name)
        raise HTTPException(status_code=404, detail="本地文件不存在")

    if not dl.startswith("http"):
        raise HTTPException(status_code=404, detail="无有效下载地址")

    fwd_headers = {"User-Agent": DEFAULT_UA}
    range_h = request.headers.get("range")
    if range_h:
        fwd_headers["Range"] = range_h

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(600.0, connect=30.0),
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", dl, headers=fwd_headers)
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"拉取远端视频失败: {exc}") from exc

    async def _iter():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    out_headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Content-Type": upstream.headers.get("content-type") or "video/mp4",
    }
    if upstream.headers.get("content-length"):
        out_headers["Content-Length"] = upstream.headers["content-length"]
    if upstream.headers.get("content-range"):
        out_headers["Content-Range"] = upstream.headers["content-range"]

    return StreamingResponse(
        _iter(),
        status_code=upstream.status_code,
        headers=out_headers,
    )


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


@app.post("/api/material/fq-koc/cache-episode")
async def cache_fq_koc_episode_quiet(body: FqKocMaterialRequest):
    """
    时间轴「缓存本集」：仅后台 HTTP 下载，不调用 Playwright（避免弹出空白浏览器页）。
    """
    from fq_koc_material import (
        cache_episode_from_url,
        find_local_material,
        local_material_decode_ok,
    )

    book_id = body.series_id.strip()
    item_id = body.item_id.strip()
    path = find_local_material(book_id, item_id)
    if path and local_material_decode_ok(path):
        rel = path.relative_to(STATIC_DIR)
        return {
            "ok": True,
            "cached": True,
            "source": "local",
            "public_url": f"/{rel.as_posix()}",
            "size": path.stat().st_size,
        }

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            result = await resolve_episode_download_url(
                client,
                book_id=book_id,
                item_id=item_id,
                try_browser=False,
            )
            if not result.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=result.get("message", "无法解析下载地址，请配置 Cookie 或手动导入 MP4"),
                )
            dl = str(result.get("download_url") or "").strip()
            if dl.startswith("/"):
                local = STATIC_DIR / dl.lstrip("/")
                if not local.is_file():
                    raise HTTPException(status_code=404, detail="本地文件不存在")
                return {
                    "ok": True,
                    "cached": True,
                    "source": result.get("source", "local"),
                    "public_url": dl,
                    "size": local.stat().st_size,
                }
            if not dl.startswith("http"):
                raise HTTPException(status_code=400, detail="未拿到有效 MP4 地址")
            path = await cache_episode_from_url(
                client,
                book_id=book_id,
                item_id=item_id,
                mp4_url=dl,
            )
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"下载失败: {exc}") from exc

    rel = path.relative_to(STATIC_DIR)
    return {
        "ok": True,
        "cached": True,
        "source": result.get("source", "download"),
        "public_url": f"/{rel.as_posix()}",
        "size": path.stat().st_size,
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


def _douyin_cache_path(aweme_id: str) -> Path:
    safe = re.sub(r"[^\d]", "", str(aweme_id or ""))
    if not safe:
        raise HTTPException(status_code=404, detail="视频不存在或已过期")
    path = DOUYIN_CACHE_DIR / f"{safe}.mp4"
    if not path.is_file() or path.stat().st_size < 10_000:
        raise HTTPException(status_code=404, detail="视频不存在或已过期，请重新爬取")
    return path


async def _finalize_douyin_mp4(path: Path) -> None:
    """重封装为浏览器可拖动的 mp4（faststart）。"""

    def work() -> None:
        from ffmpeg_util import resolve_ffmpeg_exe

        ffmpeg = resolve_ffmpeg_exe()
        tmp = path.with_suffix(".web.mp4")
        tmp.unlink(missing_ok=True)
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-map_metadata",
                "-1",
                "-movflags",
                "+faststart",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 10_000:
            path.unlink(missing_ok=True)
            tmp.rename(path)

    try:
        await asyncio.to_thread(work)
    except Exception as exc:
        logging.getLogger(__name__).info("douyin mp4 faststart skip: %s", exc)


@app.get("/preview/douyin/{aweme_id}.mp4")
async def preview_douyin_cache(aweme_id: str):
    path = _douyin_cache_path(aweme_id)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Content-Disposition": "inline", "Accept-Ranges": "bytes"},
    )


@app.get("/downloads/douyin/{aweme_id}.mp4")
async def download_douyin_cache(aweme_id: str):
    path = _douyin_cache_path(aweme_id)
    safe_aweme = re.sub(r"[^\d]", "", str(aweme_id or "")) or "video"
    name = f"douyin_{safe_aweme}.mp4"
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=name,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/tools/douyin-cache")
async def cache_douyin_from_share(body: DouyinCacheRequest):
    """主页工具：从分享文案提取 http 链接并直接爬取下载。"""
    from douyin_crawler import crawl_and_download, extract_all_http_urls

    share_text = (body.share_url or "").strip()
    if not share_text:
        raise HTTPException(status_code=400, detail="请粘贴分享文案或链接")
    if not extract_all_http_urls(share_text):
        raise HTTPException(
            status_code=400,
            detail="文案中未找到 http 链接，请粘贴含 https:// 的分享内容",
        )

    share = (
        extract_douyin_share_url(share_text)
        or extract_toutiao_share_url(share_text)
        or extract_kuaishou_share_url(share_text)
        or extract_xhs_share_url(share_text)
        or extract_all_http_urls(share_text)[0]
    )
    cookie = body.douyin_cookie.strip() or effective_douyin_cookie()

    DOUYIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with douyin_cookie_scope(cookie):
            async with httpx.AsyncClient(timeout=600.0) as client:
                dest = DOUYIN_CACHE_DIR / "page_cache.mp4"
                crawled = await crawl_and_download(client, share_text, dest)
                from douyin_material import _aweme_id_from_url

                aweme_id = (
                    _aweme_id_from_url(share)
                    or re.sub(r"[^\d]", "", str(crawled.get("aweme_id") or ""))
                    or dest.stem
                )
                final = DOUYIN_CACHE_DIR / f"{aweme_id}.mp4"
                if dest != final:
                    final.unlink(missing_ok=True)
                    dest.rename(final)
                await _finalize_douyin_mp4(final)
                result = {
                    "local_path": final,
                    "play_url": crawled.get("play_url"),
                    "aweme_id": aweme_id,
                    "caption": crawled.get("caption", ""),
                    "duration": crawled.get("duration", 0),
                    "crawl_method": crawled.get("crawl_method", "crawl"),
                }
                preview = crawled
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        raise HTTPException(
            status_code=400,
            detail=(
                f"视频下载被抖音拒绝（HTTP {code}）。请稍后重试，"
                "或展开填写可选 Cookie（登录 douyin.com 后 F12 复制）。"
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"网络请求失败: {exc}") from exc

    local = Path(result["local_path"])
    if not local.is_file():
        raise HTTPException(status_code=500, detail="视频已解析但保存失败")
    aweme_id = str(result.get("aweme_id") or local.stem)
    cover = str(preview.get("cover_url") or "").strip()
    preview_url = f"/preview/douyin/{aweme_id}.mp4"
    download_url = f"/downloads/douyin/{aweme_id}.mp4"
    wm_free = bool(result.get("watermark_free", True))
    method = str(result.get("crawl_method", "crawl"))
    return {
        "ok": True,
        "share_url": share,
        "aweme_id": aweme_id,
        "caption": "",
        "duration": result.get("duration"),
        "cover_url": cover,
        "local_path": str(local),
        "public_url": preview_url,
        "preview_url": preview_url,
        "download_url": download_url,
        "size": local.stat().st_size,
        "crawl_method": method,
        "watermark_free": wm_free,
    }


@app.get("/api/tools/sr-status")
async def sr_status():
    from video_enhance import SR_PIPELINE_VERSION, find_ncnn_exe, sr_available

    exe = find_ncnn_exe()
    return {
        "ok": True,
        "pipeline_version": SR_PIPELINE_VERSION,
        "available": sr_available(),
        "exe": str(exe) if exe else None,
        "mode": "frames",
    }


@app.get("/api/tools/watermark-presets")
async def list_watermark_presets():
    from watermark_remove import PRESET_LABELS

    return {
        "ok": True,
        "presets": [
            {"id": k, "label": v} for k, v in PRESET_LABELS.items()
        ],
    }


async def _set_wm_job(job_id: str, **fields: Any) -> None:
    async with WM_JOBS_LOCK:
        job = WM_JOBS.setdefault(job_id, {})
        job.update(fields)
        _save_wm_job(job_id, job)


async def _run_wm_job(
    job_id: str,
    src: Path,
    out: Path,
    *,
    position: str,
    strength: str,
    wm_method: str,
    use_custom_rect: bool,
    x: Optional[int],
    y: Optional[int],
    w: Optional[int],
    h: Optional[int],
    output_scale: str = "",
    output_fps_choice: str = "",
    enhance: str = "",
) -> None:
    from watermark_remove import remove_watermark

    from watermark_remove import wm_method as resolve_wm_method

    method = resolve_wm_method(wm_method or None)
    from video_enhance import sr_enabled

    use_sr = sr_enabled(enhance or None)
    if use_sr:
        progress = (
            "去水印后 Real-ESRGAN AI 超分中（可能 10–30 分钟，"
            "请勿重启服务或使用 --reload）…"
        )
    elif method == "inpaint":
        progress = "正在 OCR 定位水印并智能修复（逐帧计算，请耐心等待）…"
    else:
        progress = "正在 OCR 定位水印并去水印（整段重编码，请耐心等待）…"
    await _set_wm_job(job_id, status="running", progress=progress)
    try:
        meta = await asyncio.to_thread(
            remove_watermark,
            src,
            out,
            preset=position,
            strength=strength,
            method=wm_method or None,
            x=x if use_custom_rect else None,
            y=y if use_custom_rect else None,
            w=w if use_custom_rect else None,
            h=h if use_custom_rect else None,
            output_scale=output_scale or None,
            output_fps_choice=output_fps_choice or None,
            enhance=enhance or None,
        )
        await _set_wm_job(
            job_id,
            status="completed",
            progress="处理完成",
            download_url=f"/downloads/wm_{job_id}.mp4",
            preview_url=f"/preview/wm_{job_id}.mp4",
            size=out.stat().st_size,
            region=meta,
            regions=meta.get("regions") or [],
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("watermark job %s failed", job_id)
        out.unlink(missing_ok=True)
        err = str(exc)
        if len(err) > 500:
            err = err[:500] + "…"
        await _set_wm_job(
            job_id,
            status="failed",
            progress="处理失败",
            error=err,
        )
    finally:
        src.unlink(missing_ok=True)


@app.post("/api/tools/remove-watermark")
async def remove_video_watermark(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="本地 MP4/MOV 等视频"),
    position: str = Form("douyin", description="水印位置预设"),
    strength: str = Form("normal", description="处理强度：tight/normal/strong"),
    wm_method: str = Form("", description="inpaint=智能修复 / blur_cover=快速模糊"),
    output_scale: str = Form(
        "",
        description="输出分辨率：native/1080/4k（竖横屏自动）",
    ),
    output_fps: str = Form(
        "",
        description="输出帧率：native/60/120",
    ),
    enhance: str = Form(
        "",
        description="画质增强：off/sr（Real-ESRGAN AI 超分）",
    ),
    x: Optional[int] = Form(None, description="自定义区域左上角 X（像素）"),
    y: Optional[int] = Form(None, description="自定义区域左上角 Y"),
    w: Optional[int] = Form(None, description="区域宽度"),
    h: Optional[int] = Form(None, description="区域高度"),
):
    """上传视频，后台去水印，前端轮询 job 状态。"""
    from watermark_remove import PRESET_LABELS, wm_strength

    use_custom_rect = all(v is not None for v in (x, y, w, h))
    strength = wm_strength(strength)
    if not use_custom_rect and position not in PRESET_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"未知预设 {position!r}，或未填写完整自定义坐标",
        )

    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
        suffix = ".mp4"

    job_id = secrets.token_urlsafe(10)
    src = WM_UPLOAD_DIR / f"{job_id}_in{suffix}"
    out = DOWNLOAD_DIR / f"wm_{job_id}.mp4"

    try:
        total = 0
        with src.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > WM_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="视频超过 500MB 上限",
                    )
                fh.write(chunk)
    finally:
        await file.close()

    if total < 10_000:
        src.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件过小或为空")

    est_sec = max(30, int(total / (1024 * 1024) * 45))
    if position in ("doubao", "all-corners"):
        est_sec = int(est_sec * 1.3)
    scale_key = (output_scale or "").strip().lower()
    if scale_key in ("4k", "2160", "uhd", "4khd"):
        est_sec = int(est_sec * 2.2)
    fps_key = (output_fps or "").strip().lower()
    if fps_key in ("120", "120fps"):
        est_sec = int(est_sec * 1.6)
    elif fps_key in ("60", "60fps"):
        est_sec = int(est_sec * 1.15)
    if (enhance or "").strip().lower() in ("sr", "ai", "realesrgan", "超分", "1", "true", "on"):
        est_sec = int(est_sec * 4)

    await _set_wm_job(
        job_id,
        status="queued",
        progress=f"已上传，排队处理中…（约 {total // (1024 * 1024)} MB，预计 {est_sec // 60}–{(est_sec * 2) // 60 + 1} 分钟）",
        size_mb=round(total / (1024 * 1024), 1),
    )

    background_tasks.add_task(
        _run_wm_job,
        job_id,
        src,
        out,
        position=position,
        strength=strength,
        wm_method=wm_method,
        use_custom_rect=use_custom_rect,
        x=x,
        y=y,
        w=w,
        h=h,
        output_scale=output_scale,
        output_fps_choice=output_fps,
        enhance=enhance,
    )

    return {"ok": True, "job_id": job_id, "estimated_sec": est_sec}


async def _run_sr_job(
    job_id: str,
    src: Path,
    out: Path,
    *,
    output_scale: str = "1080",
    output_fps_choice: str = "",
) -> None:
    from video_enhance import super_resolve_video

    await _set_wm_job(
        job_id,
        status="running",
        progress="正在准备 Real-ESRGAN 超分…",
        progress_pct=0,
    )
    loop = asyncio.get_running_loop()

    def on_progress(msg: str, frac: float, eta_sec: int | None = None) -> None:
        fields: dict[str, Any] = {
            "status": "running",
            "progress": msg,
            "progress_pct": round(max(0.0, min(1.0, frac)) * 100, 1),
        }
        if eta_sec is not None and eta_sec > 0:
            fields["eta_sec"] = int(eta_sec)
        asyncio.run_coroutine_threadsafe(_set_wm_job(job_id, **fields), loop)

    try:
        meta = await asyncio.to_thread(
            super_resolve_video,
            src,
            out,
            output_scale=output_scale or "1080",
            output_fps_choice=output_fps_choice or None,
            on_progress=on_progress,
        )
        await _set_wm_job(
            job_id,
            status="completed",
            progress="超分完成",
            download_url=f"/downloads/sr_{job_id}.mp4",
            preview_url=f"/preview/sr_{job_id}.mp4",
            size=out.stat().st_size,
            region=meta,
            regions=[],
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("sr job %s failed", job_id)
        out.unlink(missing_ok=True)
        err = str(exc)
        if len(err) > 500:
            err = err[:500] + "…"
        await _set_wm_job(
            job_id,
            status="failed",
            progress="超分失败",
            error=err,
        )
    finally:
        src.unlink(missing_ok=True)


@app.post("/api/tools/super-resolution")
async def super_resolution_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="本地 MP4/MOV 等视频"),
    output_scale: str = Form(
        "1080",
        description="输出分辨率：native/1080/4k（竖横屏自动）",
    ),
    output_fps: str = Form(
        "",
        description="输出帧率：native/60/120",
    ),
):
    """上传视频，后台 Real-ESRGAN 超分（App / 网页均可调用）。"""
    from video_enhance import sr_available

    if not sr_available():
        raise HTTPException(
            status_code=503,
            detail="Real-ESRGAN 未就绪，请先在 Mac 上运行 ./start.sh",
        )

    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"):
        suffix = ".mp4"

    job_id = secrets.token_urlsafe(10)
    src = WM_UPLOAD_DIR / f"sr_{job_id}_in{suffix}"
    out = DOWNLOAD_DIR / f"sr_{job_id}.mp4"

    try:
        total = 0
        with src.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > WM_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="视频超过 500MB 上限",
                    )
                fh.write(chunk)
    finally:
        await file.close()

    if total < 10_000:
        src.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件过小或为空")

    est_sec = max(60, int(total / (1024 * 1024) * 60))
    scale_key = (output_scale or "").strip().lower()
    if scale_key in ("4k", "2160", "uhd", "4khd"):
        est_sec = int(est_sec * 2.2)
    fps_key = (output_fps or "").strip().lower()
    if fps_key in ("120", "120fps"):
        est_sec = int(est_sec * 1.4)
    elif fps_key in ("60", "60fps"):
        est_sec = int(est_sec * 1.1)

    await _set_wm_job(
        job_id,
        status="queued",
        progress=(
            f"已上传，排队 AI 超分…（约 {total // (1024 * 1024)} MB，"
            f"预计 {est_sec // 60}–{(est_sec * 2) // 60 + 1} 分钟）"
        ),
        size_mb=round(total / (1024 * 1024), 1),
    )

    background_tasks.add_task(
        _run_sr_job,
        job_id,
        src,
        out,
        output_scale=output_scale,
        output_fps_choice=output_fps,
    )

    return {"ok": True, "job_id": job_id, "estimated_sec": est_sec}


@app.get("/api/tools/super-resolution/job/{job_id}")
async def get_super_resolution_job(job_id: str):
    async with WM_JOBS_LOCK:
        job = WM_JOBS.get(job_id)
    if not job:
        job = _load_wm_job(job_id)
        if job:
            async with WM_JOBS_LOCK:
                WM_JOBS[job_id] = job
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")
    return {"ok": True, "job_id": job_id, **job}


@app.get("/api/tools/remove-watermark/job/{job_id}")
async def get_watermark_job(job_id: str):
    async with WM_JOBS_LOCK:
        job = WM_JOBS.get(job_id)
    if not job:
        job = _load_wm_job(job_id)
        if job:
            async with WM_JOBS_LOCK:
                WM_JOBS[job_id] = job
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")
    return {"ok": True, "job_id": job_id, **job}


@app.get("/downloads/images/{job_id}.zip")
async def download_wm_images_zip(job_id: str):
    safe = re.sub(r"[^\w\-]", "", job_id)
    if not safe:
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    path = DOWNLOAD_DIR / f"wm_images_{safe}.zip"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@app.post("/api/tools/remove-watermark-images")
async def remove_watermark_images(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(..., description="图片文件列表（可由文件夹批量选择）"),
    position: str = Form("douyin", description="水印位置预设"),
    strength: str = Form("normal", description="处理强度：tight/normal/strong"),
    x: Optional[int] = Form(None, description="自定义区域左上角 X（像素）"),
    y: Optional[int] = Form(None, description="自定义区域左上角 Y"),
    w: Optional[int] = Form(None, description="区域宽度"),
    h: Optional[int] = Form(None, description="区域高度"),
):
    from watermark_image import remove_watermark_image_bytes
    from watermark_remove import PRESET_LABELS, wm_strength

    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一张图片")
    if len(files) > 500:
        raise HTTPException(status_code=400, detail="单次最多处理 500 张图片")
    use_custom_rect = all(v is not None for v in (x, y, w, h))
    if not use_custom_rect and position not in PRESET_LABELS:
        raise HTTPException(status_code=400, detail=f"未知预设 {position!r}")
    strength = wm_strength(strength)

    job_id = secrets.token_urlsafe(8)
    work_dir = WM_UPLOAD_DIR / f"img_batch_{job_id}"
    out_dir = work_dir / "out"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    ok_files = 0
    failed: list[dict[str, str]] = []
    previews: list[str] = []
    zip_path = DOWNLOAD_DIR / f"wm_images_{job_id}.zip"
    zip_path.unlink(missing_ok=True)

    try:
        for idx, uf in enumerate(files, start=1):
            name = Path(uf.filename or f"image_{idx}.png").name
            raw = await uf.read()
            await uf.close()
            if not raw:
                failed.append({"name": name, "error": "空文件"})
                continue
            total_bytes += len(raw)
            if total_bytes > 300 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="图片总大小超过 300MB 上限")
            try:
                out_bytes, ext, _meta = await asyncio.to_thread(
                    remove_watermark_image_bytes,
                    raw,
                    name,
                    preset=position,
                    strength=strength,
                    x=x if use_custom_rect else None,
                    y=y if use_custom_rect else None,
                    w=w if use_custom_rect else None,
                    h=h if use_custom_rect else None,
                )
                out_name = f"{Path(name).stem}_wm{ext}"
                out_file = out_dir / out_name
                out_file.write_bytes(out_bytes)
                ok_files += 1
                if len(previews) < 6:
                    previews.append(out_name)
            except Exception as exc:
                failed.append({"name": name, "error": str(exc)})

        if ok_files < 1:
            raise HTTPException(status_code=400, detail="没有可处理的图片，请检查文件格式")

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(out_dir.iterdir()):
                if p.is_file():
                    zf.write(p, arcname=p.name)

        async def _cleanup() -> None:
            await asyncio.sleep(DOWNLOAD_TTL_SEC)
            zip_path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)

        background_tasks.add_task(_cleanup)
        return {
            "ok": True,
            "job_id": job_id,
            "processed": ok_files,
            "failed": failed,
            "zip_url": f"/downloads/images/{job_id}.zip",
            "preview_names": previews,
            "zip_size": zip_path.stat().st_size if zip_path.is_file() else 0,
        }
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        raise


@app.post("/api/tools/remove-watermark-image")
async def remove_watermark_image_single(
    file: UploadFile = File(..., description="单张图片"),
    position: str = Form("douyin", description="水印位置预设"),
    strength: str = Form("normal", description="处理强度：tight/normal/strong"),
    x: Optional[int] = Form(None),
    y: Optional[int] = Form(None),
    w: Optional[int] = Form(None),
    h: Optional[int] = Form(None),
):
    """单张图片去水印，直接返回处理后的图片字节（供 iOS 相册保存）。"""
    from watermark_image import remove_watermark_image_bytes
    from watermark_remove import PRESET_LABELS, wm_strength

    use_custom_rect = all(v is not None for v in (x, y, w, h))
    if not use_custom_rect and position not in PRESET_LABELS:
        raise HTTPException(status_code=400, detail=f"未知预设 {position!r}")
    strength = wm_strength(strength)

    name = Path(file.filename or "image.png").name
    raw = await file.read()
    await file.close()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="单张图片不能超过 30MB")

    try:
        out_bytes, ext, meta = await asyncio.to_thread(
            remove_watermark_image_bytes,
            raw,
            name,
            preset=position,
            strength=strength,
            x=x if use_custom_rect else None,
            y=y if use_custom_rect else None,
            w=w if use_custom_rect else None,
            h=h if use_custom_rect else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    media = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext.lower(), "application/octet-stream")
    out_name = f"{Path(name).stem}_wm{ext}"
    return Response(
        content=out_bytes,
        media_type=media,
        headers={
            "Content-Disposition": _content_disposition("inline", out_name, fallback="image_wm"),
            "X-Wm-Method": str(meta.get("method", "") or ""),
        },
    )


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


@app.get("/api/tts/voices")
async def tts_voice_presets():
    """返回可选 TTS 音色预设（key -> edge-tts voice）。"""
    from edge_tts_narration import VOICE_PRESETS, edge_tts_available, tts_enabled

    return {
        "ok": True,
        "enabled": bool(tts_enabled() and edge_tts_available()),
        "voices": VOICE_PRESETS,
    }


def _safe_voice_test_name(voice_key: str) -> str:
    v = (voice_key or "").strip().lower()
    return re.sub(r"[^\w-]+", "_", v)[:32] or "voice"


@app.get("/api/tts/voice-test.mp3")
async def tts_voice_test_mp3(
    text: str = Query(
        default="这段声音用来试听不同的配音风格，你觉得哪个更好听？",
        min_length=1,
        max_length=120,
        description="试听文本",
    ),
    voice: str = Query(
        default="xiaoyi",
        min_length=1,
        max_length=32,
        description="音色 key（如 xiaoyi/xiaoxiao/yunyang）",
    ),
):
    """生成单个音色的试听 mp3（带缓存）。"""
    from edge_tts_narration import edge_tts_available, synthesize_to_file, tts_enabled

    if not tts_enabled() or not edge_tts_available():
        raise HTTPException(status_code=503, detail="TTS 未启用或未安装 edge-tts")

    import hashlib

    vkey = _safe_voice_test_name(voice)
    key = hashlib.md5(f"{vkey}:{text}".encode("utf-8")).hexdigest()[:18]
    path = TTS_CACHE_DIR / f"voice_test_{vkey}_{key}.mp3"
    if not path.is_file() or path.stat().st_size < 200:
        ok = await synthesize_to_file(text, path, voice=vkey, preserve_brackets=True)
        if not ok:
            raise HTTPException(status_code=500, detail="语音合成失败")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=path.name,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/tts/voice-test")
async def tts_voice_test_batch(
    text: str = Query(
        default="这段声音用来试听不同的配音风格，你觉得哪个更好听？",
        min_length=1,
        max_length=120,
        description="试听文本",
    )
):
    """一次生成多音色试听，返回可直接访问的 mp3 URL 列表。"""
    from edge_tts_narration import VOICE_PRESETS, edge_tts_available, tts_enabled

    if not tts_enabled() or not edge_tts_available():
        raise HTTPException(status_code=503, detail="TTS 未启用或未安装 edge-tts")

    # 去重：同一个 voice id 只生成一次
    seen_voice_ids: set[str] = set()
    keys: list[str] = []
    for k, vid in VOICE_PRESETS.items():
        if vid in seen_voice_ids:
            continue
        seen_voice_ids.add(vid)
        keys.append(k)

    samples = []
    for k in keys:
        url = f"/api/tts/voice-test.mp3?voice={k}&text={httpx.QueryParams({'t': text})['t']}"
        samples.append({"voice_key": k, "url": url})
    return {"ok": True, "text": text, "samples": samples}


@app.get("/api/generate/splash-preview.png")
async def splash_preview(
    keyword: str = Query(
        ...,
        min_length=1,
        max_length=40,
        description="片头关键词，显示为《关键词》",
    ),
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
            keyword.strip(),
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


@app.post("/api/generate/job/{job_id}/interrupt-for-edit")
async def interrupt_generate_for_edit(job_id: str):
    """生成过程中中断：正片已下载后进入手动时间轴（任务状态变为 awaiting_manual_edit）。"""
    if not re.fullmatch(r"[\w-]{8,64}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    async with GENERATE_JOBS_LOCK:
        job = GENERATE_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        if job.get("status") != "running":
            raise HTTPException(
                status_code=400,
                detail="当前任务未在运行，无法中断（可能已完成或已中断）",
            )
        job["interrupt_for_edit"] = True
        phase = job.get("phase") or ""
    logging.getLogger(__name__).info(
        "job %s interrupt_for_edit=1 (phase=%s)", job_id, phase
    )
    progress = (
        "已收到中断请求，正片下载完成后将自动进入时间轴…"
        if phase not in ("materials_ready", "awaiting_manual_edit")
        else "正在中断并载入时间轴草稿…"
    )
    await _set_job(job_id, progress=progress)
    return {"ok": True, "job_id": job_id, "phase": phase, "progress": progress}


@app.get("/api/generate/job/{job_id}")
async def get_generate_job(job_id: str):
    if not re.fullmatch(r"[\w-]{8,64}", job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    async with GENERATE_JOBS_LOCK:
        job = GENERATE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"ok": True, "job_id": job_id, **job}


@app.post("/api/manual/edit-plan-draft")
async def manual_edit_plan_draft(body: ManualEditPlanDraftRequest):
    """手動多段剪輯：生成可編輯草稿（可預填兩段高光）。"""
    from fq_koc_material import local_material_duration
    from manual_edit_plan import build_manual_draft_with_prefill

    if not body.episode_item_ids:
        raise HTTPException(status_code=400, detail="请至少选择一集")
    labels: list[str] = []
    durations: list[float] = []
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            for index, item_id in enumerate(body.episode_item_ids, start=1):
                labels.append(body.episode_titles.get(item_id) or f"第{index}集")
                dur_local = local_material_duration(body.series_id, item_id)
                if dur_local > 1:
                    durations.append(dur_local)
                    continue
                try:
                    info = await fetch_episode_video_info(
                        client, item_id, book_id=body.series_id
                    )
                    durations.append(duration_from_episode_info(info))
                except Exception:
                    durations.append(120.0)
        draft = await build_manual_draft_with_prefill(
            series_id=body.series_id,
            drama_title=body.drama_title or body.series_id,
            opening=body.opening.strip(),
            keyword=body.keyword.strip(),
            episode_labels=labels,
            episode_durations=durations,
            episode_item_ids=body.episode_item_ids,
            prefill=body.prefill,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logging.getLogger(__name__).exception("manual edit-plan-draft failed")
        raise HTTPException(status_code=500, detail=str(exc)[:500]) from exc
    return {"ok": True, "edit_plan": draft}


@app.post("/api/ai/edit-plan")
async def preview_ai_edit_plan(body: AiEditPlanRequest):
    """仅生成 AI 剪辑方案，不下载/合成视频。"""
    labels: list[str] = []
    durations: list[float] = []
    async with httpx.AsyncClient(timeout=120.0) as client:
        for index, item_id in enumerate(body.episode_item_ids, start=1):
            labels.append(body.episode_titles.get(item_id) or f"第{index}集")
            try:
                info = await fetch_episode_video_info(
                    client, item_id, book_id=body.series_id
                )
                durations.append(duration_from_episode_info(info))
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


# 静态资源：放在所有 API 路由之后
if STATIC_DIR.is_dir():
    _materials_root = STATIC_DIR / "materials"
    if _materials_root.is_dir():
        app.mount(
            "/materials",
            StaticFiles(directory=str(_materials_root)),
            name="materials",
        )

    app.mount(
        "/assets",
        StaticFiles(directory=str(STATIC_DIR)),
        name="assets",
    )

    _cache_root = STATIC_DIR / "cache"
    if _cache_root.is_dir():
        app.mount(
            "/cache",
            StaticFiles(directory=str(_cache_root)),
            name="cache",
        )

    _icons_root = STATIC_DIR / "icons"
    if _icons_root.is_dir():
        app.mount(
            "/icons",
            StaticFiles(directory=str(_icons_root)),
            name="icons",
        )

    @app.get("/")
    async def index_page():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/generate.html")
    async def generate_page():
        return FileResponse(STATIC_DIR / "generate.html")

    @app.get("/favicon.ico")
    async def favicon():
        icon = STATIC_DIR / "icons" / "icon-192.png"
        if icon.is_file():
            return FileResponse(icon, media_type="image/png")
        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/{filename}")
    async def public_file(filename: str):
        if "/" in filename or ".." in filename or filename == "api":
            raise HTTPException(status_code=404, detail="Not Found")
        path = STATIC_DIR / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path)
