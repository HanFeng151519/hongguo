"""番茄达人中心（koc.fqopenplatform.com）批量下载明文正片。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

import httpx

from material_common import (
    _ffmpeg_bin,
    apply_fanqie_vod_query,
    download_http_video,
    is_usable_video_file,
    probe_decodes,
)
from video_drm import video_decodes

logger = logging.getLogger(__name__)

KOC_BASE = "https://koc.fqopenplatform.com"
CREATE_PATH = "/api/platform/content/batch_download/create/v1"


def build_koc_book_detail_url(book_id: str, item_id: str) -> str:
    """
    机构/邀请链接下的分集详情页（Playwright 须打开此页才能完成认证与下载）。
    参数与浏览器地址栏一致：invite_user_share_token、key、genre、book_id、item_id。
    """
    invite = os.getenv("HONGGUO_FQ_KOC_INVITE_TOKEN", "").strip()
    if not invite:
        raise RuntimeError(
            "未配置 HONGGUO_FQ_KOC_INVITE_TOKEN：从推广中心 book-detail 地址栏复制 "
            "invite_user_share_token=... 整段值到 .env"
        )
    q = {
        "tab_type": os.getenv("HONGGUO_FQ_KOC_TAB_TYPE", "6"),
        "top_tab_genre": os.getenv("HONGGUO_FQ_KOC_TOP_TAB_GENRE", "-1"),
        "invite_user_share_token": invite,
        "key": os.getenv("HONGGUO_FQ_KOC_DETAIL_KEY", "205_0"),
        "genre": os.getenv("HONGGUO_FQ_KOC_GENRE", "205"),
        "book_id": book_id,
        "item_id": item_id,
    }
    return f"{KOC_BASE}/page/member/content/book-detail?{urlencode(q)}"


def koc_referer_url(book_id: str = "", item_id: str = "") -> str:
    if book_id and item_id:
        try:
            return build_koc_book_detail_url(book_id, item_id)
        except RuntimeError:
            pass
    tab = os.getenv("HONGGUO_FQ_KOC_TAB_TYPE", "6")
    invite = os.getenv("HONGGUO_FQ_KOC_INVITE_TOKEN", "").strip()
    q: dict[str, str] = {"tab_type": tab}
    if invite:
        q["invite_user_share_token"] = invite
    return f"{KOC_BASE}/page/member/content?{urlencode(q)}"
QUERY_PATHS = (
    "/api/platform/content/batch_download/query/v1",
    "/api/platform/content/batch_download/status/v1",
    "/api/platform/content/batch_download/result/v1",
)
LIST_PATHS = (
    "/api/platform/content/list/v1",
    "/api/platform/content/page/v1",
    "/api/platform/member/content/list/v1",
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MATERIAL_DIR = (
    Path(__file__).resolve().parent.parent / "public" / "materials" / "fq_koc"
)


def _cache_path(book_id: str, item_id: str) -> Path:
    return MATERIAL_DIR / f"{book_id}_{item_id}.mp4"


def _link_or_copy(src: Path, dest: Path) -> None:
    """同盘硬链避免重复拷贝大文件（约 60MB）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _persist_to_cache(src: Path, book_id: str, item_id: str) -> Path:
    """在线下载成功后写入本地缓存，下次生成秒开。"""
    cache = _cache_path(book_id, item_id)
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    if src.resolve() == cache.resolve():
        return cache
    if cache.exists():
        cache.unlink()
    _link_or_copy(src, cache)
    logger.info("已缓存达人中心素材: %s", cache.name)
    return cache


def _cookie() -> str:
    return os.getenv("HONGGUO_FQ_KOC_COOKIE", "").strip()


def _ms_token() -> str:
    return (
        os.getenv("_HONGGUO_FQ_KOC_MS_TOKEN_REQUEST", "").strip()
        or os.getenv("HONGGUO_FQ_KOC_MS_TOKEN", "").strip()
    )


def _a_bogus() -> str:
    return (
        os.getenv("_HONGGUO_FQ_KOC_A_BOGUS_REQUEST", "").strip()
        or os.getenv("HONGGUO_FQ_KOC_A_BOGUS", "").strip()
    )


def _app_id() -> str:
    return os.getenv("HONGGUO_FQ_KOC_APP_ID", "457699").strip() or "457699"


def _create_url_override() -> str:
    return (
        os.getenv("HONGGUO_FQ_KOC_CREATE_URL", "").strip()
        or os.getenv("_HONGGUO_FQ_KOC_CREATE_URL_REQUEST", "").strip()
    )


def _apply_tokens_from_url(url: str) -> None:
    if not url:
        return
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if qs.get("msToken"):
        os.environ["HONGGUO_FQ_KOC_MS_TOKEN"] = qs["msToken"][0]
    if qs.get("a_bogus"):
        os.environ["HONGGUO_FQ_KOC_A_BOGUS"] = qs["a_bogus"][0]
    for key in ("app_id", "aid", "origin_app_id", "host_app_id"):
        if qs.get(key):
            os.environ[f"HONGGUO_FQ_KOC_{key.upper()}"] = qs[key][0]


def set_request_koc_options(
    *,
    create_url: str = "",
    cookie: str = "",
    ms_token: str = "",
    a_bogus: str = "",
    download_body: str = "",
    direct_mp4_url: str = "",
) -> None:
    """单次生成请求可覆盖达人中心 URL / Cookie / 签名 / Payload。"""
    if create_url.strip():
        os.environ["_HONGGUO_FQ_KOC_CREATE_URL_REQUEST"] = create_url.strip()
        _apply_tokens_from_url(create_url.strip())
    if ms_token.strip():
        os.environ["_HONGGUO_FQ_KOC_MS_TOKEN_REQUEST"] = ms_token.strip()
    if a_bogus.strip():
        os.environ["_HONGGUO_FQ_KOC_A_BOGUS_REQUEST"] = a_bogus.strip()
    if cookie.strip():
        os.environ["_HONGGUO_FQ_KOC_COOKIE_REQUEST"] = cookie.strip()
    if download_body.strip():
        os.environ["_HONGGUO_FQ_KOC_DOWNLOAD_BODY_REQUEST"] = download_body.strip()
    if direct_mp4_url.strip():
        os.environ["_HONGGUO_FQ_KOC_DIRECT_MP4_URL_REQUEST"] = direct_mp4_url.strip()

    from fq_koc_session import save_koc_session

    if any(
        (
            create_url.strip(),
            cookie.strip(),
            ms_token.strip(),
            a_bogus.strip(),
            download_body.strip(),
        )
    ):
        save_koc_session(
            create_url=create_url.strip(),
            cookie=cookie.strip(),
            ms_token=ms_token.strip(),
            a_bogus=a_bogus.strip(),
            download_body=download_body.strip(),
        )


def clear_request_koc_options() -> None:
    for key in (
        "_HONGGUO_FQ_KOC_CREATE_URL_REQUEST",
        "_HONGGUO_FQ_KOC_COOKIE_REQUEST",
        "_HONGGUO_FQ_KOC_MS_TOKEN_REQUEST",
        "_HONGGUO_FQ_KOC_A_BOGUS_REQUEST",
        "_HONGGUO_FQ_KOC_DOWNLOAD_BODY_REQUEST",
        "_HONGGUO_FQ_KOC_DIRECT_MP4_URL_REQUEST",
    ):
        os.environ.pop(key, None)


def _download_body_raw() -> str:
    return (
        os.getenv("_HONGGUO_FQ_KOC_DOWNLOAD_BODY_REQUEST", "").strip()
        or os.getenv("HONGGUO_FQ_KOC_DOWNLOAD_BODY", "").strip()
    )


def _is_form_body(raw: str) -> bool:
    s = raw.strip()
    return bool(s) and "=" in s and not s.startswith("{")


def _download_body_template() -> Optional[dict[str, Any]]:
    raw = _download_body_raw()
    if not raw or _is_form_body(raw):
        return None
    return json.loads(raw)


def _download_form_template(book_id: str, item_id: str) -> Optional[dict[str, str]]:
    raw = _download_body_raw()
    if not raw or not _is_form_body(raw):
        return None
    text = (
        raw.replace("{book_id}", book_id)
        .replace("{item_id}", item_id)
        .replace("{item_ids}", item_id)
    )
    return {k: v for k, v in parse_qsl(text, keep_blank_values=True)}


def _browser_form_payload(
    book_id: str, item_id: str, *, merge_tokens: bool = True
) -> dict[str, str]:
    """与推广中心页面点击「下载」一致的 x-www-form-urlencoded 字段。"""
    app = _app_id()
    form: dict[str, str] = {
        "book_id": book_id,
        "item_id": item_id,
        "content_tab": "6",
        "app_id": app,
        "aid": app,
        "origin_app_id": app,
        "host_app_id": app,
    }
    if merge_tokens:
        if _ms_token():
            form["msToken"] = _ms_token()
        if _a_bogus():
            form["a_bogus"] = _a_bogus()
    return form


def _apply_form_tokens(form: dict[str, str]) -> None:
    ms = form.get("msToken", "").strip()
    ab = form.get("a_bogus", "").strip()
    if ms:
        os.environ["HONGGUO_FQ_KOC_MS_TOKEN"] = ms
    if ab:
        os.environ["HONGGUO_FQ_KOC_A_BOGUS"] = ab


def _cookie_effective() -> str:
    return (
        os.getenv("_HONGGUO_FQ_KOC_COOKIE_REQUEST", "").strip() or _cookie()
    )


def is_configured() -> bool:
    return bool(_cookie_effective())


def config_status() -> dict[str, Any]:
    url = _create_url_override()
    _apply_tokens_from_url(url)
    cookie_ok = bool(_cookie_effective())
    from fq_koc_browser import _auto_sync_enabled

    return {
        "cookie": cookie_ok,
        "create_url": bool(url),
        "ms_token": bool(_ms_token()),
        "a_bogus": bool(_a_bogus()),
        "has_download_body": bool(
            _download_body_raw()
            and (
                _download_body_template() is not None
                or _is_form_body(_download_body_raw())
            )
        ),
        "has_vod_query": bool(os.getenv("HONGGUO_FQ_KOC_VOD_QUERY", "").strip()),
        "auto_sync": _auto_sync_enabled(),
        "ready": cookie_ok,
    }


def material_help_messages(
    book_id: str = "", item_id: str = ""
) -> list[str]:
    """给人看的配置/权限说明（不含密钥内容）。"""
    msgs: list[str] = []
    if book_id and item_id and find_local_material(book_id, item_id):
        path = find_local_material(book_id, item_id)
        size_mb = path.stat().st_size / (1024 * 1024) if path else 0
        msgs.append(
            f"已找到本地 MP4（{path.name}，{size_mb:.1f}MB），生成时直接硬链，不再重复下载。"
        )
        if path and not local_material_decode_ok(path):
            msgs.append(
                "警告：该文件 ffmpeg 无法解码，可能下载不完整或已损坏，"
                "请在推广中心重新下载并覆盖同名文件。"
            )
        else:
            msgs.append(
                "仅在线拉取其它分集时才需 Cookie / msToken / DOWNLOAD_BODY。"
            )
        return msgs

    st = config_status()
    if not st["cookie"]:
        msgs.append(
            "未配置 Cookie：登录 https://koc.fqopenplatform.com 后，"
            "F12 → Network → 复制请求头 Cookie 到 .env 的 HONGGUO_FQ_KOC_COOKIE。"
        )
    else:
        msgs.append("Cookie 已配置。")

    if not st["ms_token"]:
        msgs.append(
            "未配置 msToken：从 batch_download/create 请求 URL 复制到 HONGGUO_FQ_KOC_MS_TOKEN。"
        )
    else:
        msgs.append("msToken 已配置（若仍失败，多半已过期，请重新抓包更新）。")

    if not st["a_bogus"]:
        msgs.append(
            "未配置 a_bogus：从同一请求 URL 复制到 HONGGUO_FQ_KOC_A_BOGUS。"
        )
    else:
        msgs.append("a_bogus 已配置（与 msToken 一样会过期）。")

    try:
        from fq_koc_session import session_public_status

        sess = session_public_status()
        if sess.get("loaded"):
            msgs.append(
                f"已加载自动保存的抓包（{sess.get('path')}，"
                f"更新 {sess.get('updated_at') or '未知'}）；"
                "msToken 会在请求后自动刷新。"
            )
    except Exception:
        pass

    if not st["has_download_body"]:
        msgs.append(
            "未配置下载 Payload：推广中心点「下载」→ F12 → Copy as cURL →"
            " 生成页「导入 F12 抓包」，或填写 DOWNLOAD_BODY（占位符 {book_id}、{item_id}）。"
        )
    else:
        msgs.append("DOWNLOAD_BODY 已配置。")

    if book_id and item_id and not find_local_material(book_id, item_id):
        from fq_koc_browser import _auto_sync_enabled

        if _auto_sync_enabled():
            msgs.append(
                f"第 {item_id} 集无缓存时：接口失败会尝试 Playwright（AUTO_SYNC=1）。"
            )
        else:
            msgs.append(
                f"第 {item_id} 集无缓存：请更新 .env 的 Cookie/msToken/a_bogus，"
                "或生成页「导入 F12 抓包」/ 填写本集 CDN 直链；"
                "勿弹浏览器可保持 HONGGUO_FQ_KOC_AUTO_SYNC=0。"
            )
    return msgs


def raise_material_download_error(
    *,
    book_id: str,
    item_id: str,
    api_errors: Optional[list[str]] = None,
) -> None:
    if api_errors:
        detail = api_errors[-1][:500]
        if any(
            kw in detail
            for kw in ("Playwright", "未安装", "无法启动浏览器")
        ):
            raise RuntimeError(
                f"第 {item_id} 集自动拉片需要 Playwright：\n"
                "  pip3 install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
                "（使用本机 Chrome，不必 playwright install chromium）\n\n"
                f"{detail}"
            )
        raise RuntimeError(
            f"第 {item_id} 集下载失败：{detail}\n\n"
            "建议：在 .env 更新 HONGGUO_FQ_KOC_COOKIE、MS_TOKEN、A_BOGUS，"
            "或生成页「导入 F12 抓包」；换集可填 HONGGUO_FQ_KOC_DIRECT_MP4_URL。"
            "若确需自动弹 Chrome，设 HONGGUO_FQ_KOC_AUTO_SYNC=1。"
        )
    raise RuntimeError("\n".join(material_help_messages(book_id, item_id)))


def _headers() -> dict[str, str]:
    h = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Referer": koc_referer_url(),
        "Origin": KOC_BASE,
    }
    cookie = _cookie_effective()
    if cookie:
        h["Cookie"] = cookie
    ms = _ms_token()
    if ms:
        h["x-ms-token"] = ms
    if cookie:
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("passport_csrf_token=") and "default" not in part:
                h["x-secsdk-csrf-token"] = part.split("=", 1)[1]
                break
    return h


_episode_item_to_index: dict[str, dict[str, int]] = {}


def episode_map_path(book_id: str) -> Path:
    return MATERIAL_DIR / f"{book_id}_episodes.json"


def save_episode_map(book_id: str, episodes: list[dict[str, Any]]) -> Path:
    """保存 item_id ↔ 集数，供识别「第6集.mp4」等别名文件。"""
    rows: list[dict[str, Any]] = []
    for index, ep in enumerate(episodes, start=1):
        iid = str(ep.get("item_id") or "").strip()
        if not iid:
            continue
        rows.append(
            {
                "index": index,
                "title": str(ep.get("title") or f"第{index}集"),
                "item_id": iid,
            }
        )
    payload = {"book_id": book_id, "episodes": rows}
    path = episode_map_path(book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _episode_item_to_index.pop(book_id, None)
    return path


def _episode_index_map(book_id: str) -> dict[str, int]:
    if book_id in _episode_item_to_index:
        return _episode_item_to_index[book_id]
    mapping: dict[str, int] = {}
    path = episode_map_path(book_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for row in data.get("episodes") or []:
                if not isinstance(row, dict):
                    continue
                iid = str(row.get("item_id") or "").strip()
                idx = int(row.get("index") or 0)
                if iid and idx > 0:
                    mapping[iid] = idx
        except Exception as exc:
            logger.warning("读取剧集映射 %s 失败: %s", path.name, exc)
    _episode_item_to_index[book_id] = mapping
    return mapping


def _title_named_episode_paths(book_id: str, item_id: str) -> list[Path]:
    """匹配手动下载的「第N集.mp4」。"""
    out: list[Path] = []
    idx = _episode_index_map(book_id).get(item_id)
    if not idx:
        return out
    for name in (f"第{idx}集.mp4", f"第{idx:02d}集.mp4", f"第{idx}集.MP4"):
        out.append(MATERIAL_DIR / name)
    return out


def link_title_named_materials(book_id: str) -> list[str]:
    """
    为「第N集.mp4」创建标准名硬链 {book_id}_{item_id}.mp4（不复制文件）。
    返回已创建/已存在的链接说明。
    """
    notes: list[str] = []
    ep_map = _episode_index_map(book_id)
    if not ep_map:
        return ["未找到剧集映射文件，请先保存 episode_map 或访问分集列表"]
    for item_id, idx in ep_map.items():
        src = MATERIAL_DIR / f"第{idx}集.mp4"
        if not src.is_file():
            alt = MATERIAL_DIR / f"第{idx:02d}集.mp4"
            if alt.is_file():
                src = alt
        if not src.is_file():
            continue
        dest = _cache_path(book_id, item_id)
        if dest.is_file():
            notes.append(f"已有标准名: {dest.name}")
            continue
        try:
            os.link(src, dest)
            notes.append(f"已链接: {src.name} → {dest.name}")
        except OSError as exc:
            logger.warning("硬链 %s: %s", dest.name, exc)
            notes.append(f"链接失败 {src.name}: {exc}")
    return notes


def _local_material_candidates(book_id: str, item_id: str) -> list[Path]:
    extra = os.getenv("HONGGUO_LOCAL_BODY_MP4", "").strip()
    paths: list[Path] = []
    if extra:
        paths.append(Path(extra))
    paths.extend(_title_named_episode_paths(book_id, item_id))
    paths.extend(
        [
            MATERIAL_DIR / f"{book_id}_{item_id}.mp4",
            MATERIAL_DIR / f"{item_id}.mp4",
        ]
    )
    return paths


def _probe_local_duration(path: Path) -> float:
    """ffprobe 优先，失败则用 ffmpeg -i 解析 Duration 行。"""
    import shutil

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode == 0:
                raw = (proc.stdout or "").strip().splitlines()
                if raw:
                    return max(0.0, float(raw[0]))
        except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("ffprobe 时长 %s: %s", path.name, exc)
    try:
        proc = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in (proc.stderr or "").splitlines():
            if "Duration:" in line:
                part = line.split("Duration:", 1)[1].split(",")[0].strip()
                h, m, s = part.split(":")
                return float(h) * 3600 + float(m) * 60 + float(s)
    except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("ffmpeg 时长 %s: %s", path.name, exc)
    return 0.0


def local_material_duration(book_id: str, item_id: str) -> float:
    """本地推广中心素材时长（秒），无则 0。"""
    path = find_local_material(book_id, item_id)
    if not path:
        return 0.0
    return _probe_local_duration(path)


def local_material_decode_ok(path: Path) -> bool:
    """能否实际解码（比仅看文件头更可靠）。"""
    return video_decodes(path, min_frames=3)


def find_local_material(book_id: str, item_id: str) -> Optional[Path]:
    """达人中心浏览器下载的 MP4 可放到 public/materials/fq_koc/ 下复用。"""
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    for path in _local_material_candidates(book_id, item_id):
        if path.is_file() and is_usable_video_file(path):
            return path
    return None


def validate_local_material(book_id: str, item_id: str) -> None:
    """本地文件存在但不可解码时给出明确说明（与 Cookie 无关）。"""
    path = find_local_material(book_id, item_id)
    if not path:
        return
    if local_material_decode_ok(path):
        return
    raise RuntimeError(
        f"本地素材 {path.name} 已找到，但 ffmpeg 无法解码（常见原因：下载未完成、"
        "文件损坏，或并非推广中心完整明文）。这与 Cookie 无关。\n"
        "请用浏览器在推广中心重新下载该集，覆盖 "
        f"{MATERIAL_DIR}/{book_id}_{item_id}.mp4 后再试。"
    )


def _base_query() -> dict[str, str]:
    app = _app_id()
    q = {
        "app_id": app,
        "aid": app,
        "origin_app_id": app,
        "host_app_id": app,
    }
    if _ms_token():
        q["msToken"] = _ms_token()
    if _a_bogus():
        q["a_bogus"] = _a_bogus()
    return q


def _merge_url_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    merged = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    merged.update(extra)
    return urlunparse(parsed._replace(query=urlencode(merged)))


def _create_endpoint() -> tuple[str, dict[str, str]]:
    override = _create_url_override()
    if override:
        _apply_tokens_from_url(override)
        # 使用 F12 复制的完整 URL（含 a_bogus），勿用 .env 旧 token 覆盖 query
        return override.strip(), _headers()
    return f"{KOC_BASE}{CREATE_PATH}", _headers()


def _download_request_plans(
    *, book_id: str, item_id: str, item_ids: list[str]
) -> list[tuple[str, str, dict[str, str], Optional[dict[str, Any]], Optional[dict[str, str]]]]:
    """
    返回 (label, url, query_params, json_body, form_body)。
    优先浏览器表单格式：book_id + item_id + content_tab=6 + msToken + a_bogus。
    """
    base_url = f"{KOC_BASE}{CREATE_PATH}"
    plans: list[
        tuple[str, str, dict[str, str], Optional[dict[str, Any]], Optional[dict[str, str]]]
    ] = []

    custom_form = _download_form_template(book_id, item_id)
    if custom_form:
        _apply_form_tokens(custom_form)
        plans.append(("custom_form", base_url, {}, None, custom_form))

    plans.append(
        ("browser_form", base_url, {}, None, _browser_form_payload(book_id, item_id))
    )

    override = _create_url_override()
    if override:
        parsed = urlparse(override)
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        _apply_form_tokens({k: qs.get(k, "") for k in ("msToken", "a_bogus")})
        form_from_url = _browser_form_payload(book_id, item_id, merge_tokens=False)
        form_from_url.update(
            {k: qs[k] for k in qs if k not in form_from_url or k in ("msToken", "a_bogus")}
        )
        plans.append(
            (
                "form_with_create_url_tokens",
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                {},
                None,
                form_from_url,
            )
        )

    custom_json = _download_body_template()
    if custom_json is not None:
        text = json.dumps(custom_json)
        text = text.replace("{book_id}", book_id).replace("{item_id}", item_id)
        text = text.replace("{item_ids}", json.dumps(item_ids))
        plans.append(("custom_json", base_url, _base_query(), json.loads(text), None))

    ids = item_ids or ([item_id] if item_id else [])
    for label, body in (
        ("json_item_list", {"book_id": book_id, "item_id_list": ids, "tab_type": 6}),
        ("json_item_id", {"book_id": book_id, "item_id": item_id, "content_tab": 6}),
    ):
        plans.append((label, base_url, _base_query(), body, None))

    return plans


def _extract_download_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("download_url", "url", "play_url", "video_url"):
        val = payload.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_download_url(data)
    if isinstance(data, list):
        for item in data:
            u = _extract_download_url(item)
            if u:
                return u
    task = payload.get("task_info") or payload.get("task")
    if isinstance(task, dict):
        return _extract_download_url(task)
    return ""


def _extract_task_id(payload: dict[str, Any]) -> str:
    for key in ("task_id", "job_id", "download_id", "id"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    data = payload.get("data")
    if isinstance(data, dict):
        return _extract_task_id(data)
    return ""


def _parse_json_response(resp: httpx.Response) -> dict[str, Any]:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"番茄达人中心返回非 JSON（{resp.status_code}）: {resp.text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        return {"data": data}
    code = data.get("code")
    if code not in (0, None, "0") and data.get("status_code") not in (0, None):
        msg = data.get("message") or data.get("msg") or data.get("status_msg")
        if msg:
            raise RuntimeError(f"番茄达人中心接口错误: {msg}")
    return data


async def search_member_content(
    client: httpx.AsyncClient,
    keyword: str,
    *,
    tab_type: int = 6,
    page: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """在达人中心内容库搜索（需 Cookie；具体路径因版本可能不同）。"""
    if not _cookie_effective():
        raise RuntimeError("未配置 HONGGUO_FQ_KOC_COOKIE")

    body = {
        "keyword": keyword,
        "tab_type": tab_type,
        "page": page,
        "page_size": page_size,
        "query": keyword,
    }
    params = _base_query()
    params.update({"tab_type": str(tab_type), "page": str(page), "page_size": str(page_size)})

    last_err = ""
    for path in LIST_PATHS:
        url = f"{KOC_BASE}{path}"
        for method in ("POST", "GET"):
            try:
                if method == "POST":
                    resp = await client.post(
                        url, params=params, json=body, headers=_headers(), timeout=30.0
                    )
                else:
                    resp = await client.get(
                        url, params={**params, **body}, headers=_headers(), timeout=30.0
                    )
                if resp.status_code == 404:
                    continue
                payload = _parse_json_response(resp)
                items = _normalize_list_payload(payload)
                if items:
                    return items
            except Exception as exc:
                last_err = str(exc)
                logger.debug("KOC list %s %s: %s", method, path, exc)
    raise RuntimeError(
        f"达人中心内容列表接口未返回数据（{last_err or '请从浏览器 Network 确认 list 接口路径'}）"
    )


def _normalize_list_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("data") or payload.get("list") or payload.get("items") or []
    if isinstance(raw, dict):
        raw = raw.get("list") or raw.get("items") or raw.get("data") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        book_id = str(
            item.get("book_id")
            or item.get("content_id")
            or item.get("series_id")
            or ""
        )
        title = str(item.get("title") or item.get("book_name") or item.get("name") or "")
        out.append({**item, "book_id": book_id, "title": title})
    return out


async def create_batch_download(
    client: httpx.AsyncClient,
    *,
    book_id: str,
    item_id: str,
    item_ids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """创建批量下载任务，返回解析后的 JSON（含 download_url 或 task_id）。"""
    if not _cookie_effective():
        raise_material_download_error(book_id=book_id, item_id=item_id)
    if not book_id:
        raise RuntimeError("缺少 book_id / series_id")

    ids = item_ids or ([item_id] if item_id else [])
    errors: list[str] = []

    from fq_koc_session import save_koc_session

    for label, url, params, json_body, form_body in _download_request_plans(
        book_id=book_id, item_id=item_id, item_ids=ids
    ):
        try:
            headers = _headers()
            if form_body is not None:
                headers = {
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                }
            resp = None
            for attempt in range(3):
                if form_body is not None:
                    resp = await client.post(
                        url,
                        params=params or None,
                        data=form_body,
                        headers=headers,
                        timeout=60.0,
                    )
                else:
                    resp = await client.post(
                        url,
                        params=params or None,
                        json=json_body,
                        headers=headers,
                        timeout=60.0,
                    )
                new_ms = resp.headers.get("x-ms-token") or resp.headers.get(
                    "X-Ms-Token"
                )
                if new_ms:
                    os.environ["HONGGUO_FQ_KOC_MS_TOKEN"] = new_ms
                    os.environ["_HONGGUO_FQ_KOC_MS_TOKEN_REQUEST"] = new_ms
                    headers = _headers()
                    if form_body is not None:
                        form_body["msToken"] = new_ms
                        headers["Content-Type"] = (
                            "application/x-www-form-urlencoded;charset=UTF-8"
                        )
                    save_koc_session(ms_token=new_ms)
                if resp.content or resp.status_code != 200:
                    break
                if not new_ms:
                    break
            if resp is None:
                continue
            if resp.status_code == 404:
                raise RuntimeError(f"下载接口 404: {url}")
            if not resp.content:
                errors.append(f"空响应 [{label}]")
                continue
            payload = _parse_json_response(resp)
            download_url = _extract_download_url(payload)
            task_id = _extract_task_id(payload)
            saved_body: Any = form_body if form_body is not None else json_body
            if download_url or task_id:
                if form_body:
                    save_koc_session(
                        download_body=urlencode(form_body),
                        ms_token=form_body.get("msToken", ""),
                        a_bogus=form_body.get("a_bogus", ""),
                        last_working_body=dict(form_body),
                    )
                else:
                    save_koc_session(last_working_body=json_body)
                return {
                    **payload,
                    "download_url": download_url,
                    "task_id": task_id,
                    "_request_body": saved_body,
                }
            if payload.get("data") is not None:
                return {
                    **payload,
                    "download_url": download_url,
                    "task_id": task_id,
                    "_request_body": saved_body,
                }
            errors.append(f"无下载字段 [{label}] keys={list(payload.keys())[:8]}")
        except RuntimeError as exc:
            errors.append(str(exc))
        except httpx.HTTPError as exc:
            errors.append(str(exc))

    raise_material_download_error(
        book_id=book_id, item_id=item_id, api_errors=errors
    )


async def poll_batch_download(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    max_wait_sec: int = 45,
) -> str:
    """轮询批量下载任务，返回最终 mp4 地址。"""
    if not task_id:
        return ""
    deadline = time.time() + max_wait_sec
    params = {**_base_query(), "task_id": task_id, "download_id": task_id}
    body = {"task_id": task_id, "download_id": task_id}

    while time.time() < deadline:
        for path in QUERY_PATHS:
            url = f"{KOC_BASE}{path}"
            try:
                resp = await client.post(
                    url, params=params, json=body, headers=_headers(), timeout=30.0
                )
                if resp.status_code == 404:
                    resp = await client.get(
                        url, params=params, headers=_headers(), timeout=30.0
                    )
                if resp.status_code == 404:
                    continue
                payload = _parse_json_response(resp)
                url_found = _extract_download_url(payload)
                if url_found:
                    return url_found
                status = str(
                    (payload.get("data") or {}).get("status")
                    or payload.get("status")
                    or ""
                ).lower()
                if status in ("failed", "error", "fail"):
                    raise RuntimeError(f"达人中心下载任务失败: {payload}")
            except RuntimeError:
                raise
            except Exception as exc:
                logger.debug("poll %s: %s", path, exc)
        await asyncio.sleep(2)

    return ""


async def resolve_download_url(
    client: httpx.AsyncClient,
    create_result: dict[str, Any],
) -> str:
    direct = str(create_result.get("download_url") or "")
    if direct.startswith("http"):
        return direct
    task_id = str(create_result.get("task_id") or "")
    if task_id:
        polled = await poll_batch_download(client, task_id)
        if polled:
            return polled
    return _extract_download_url(create_result)


def _direct_mp4_url(item_id: str) -> str:
    """.env 中配置 CDN 直链时跳过 batch_download API。"""
    req = os.getenv("_HONGGUO_FQ_KOC_DIRECT_MP4_URL_REQUEST", "").strip()
    if req.startswith("http"):
        return req
    specific = os.getenv(f"HONGGUO_FQ_KOC_MP4_URL_{item_id}", "").strip()
    if specific.startswith("http"):
        return specific
    url = os.getenv("HONGGUO_FQ_KOC_DIRECT_MP4_URL", "").strip()
    if not url.startswith("http"):
        return ""
    bind = os.getenv("HONGGUO_FQ_KOC_DIRECT_ITEM_ID", "").strip()
    if bind and bind != item_id:
        return ""
    return url


async def resolve_episode_download_url(
    client: httpx.AsyncClient,
    *,
    book_id: str,
    item_id: str,
    try_browser: bool = False,
) -> dict[str, Any]:
    """
    解析单集明文 MP4 地址（不下载文件）。
    返回 source: local | batch | direct | browser | need_capture
    """
    cached = find_local_material(book_id, item_id)
    if cached and local_material_decode_ok(cached):
        rel = cached.relative_to(
            Path(__file__).resolve().parent.parent / "public"
        )
        return {
            "ok": True,
            "source": "local",
            "download_url": f"/{rel.as_posix()}",
            "cached": True,
            "size": cached.stat().st_size,
        }

    direct = _direct_mp4_url(item_id)
    if direct.startswith("http"):
        return {
            "ok": True,
            "source": "direct",
            "download_url": apply_fanqie_vod_query(direct),
            "cached": False,
        }

    if not _cookie_effective():
        return {
            "ok": False,
            "source": "need_capture",
            "message": "未配置 Cookie，请在推广中心打开助手脚本或导入 F12 抓包",
        }

    last_err = ""
    for _ in range(2):
        try:
            if not _ms_token() or not _a_bogus():
                raise RuntimeError("缺少 msToken/a_bogus")
            create_result = await create_batch_download(
                client, book_id=book_id, item_id=item_id
            )
            play_url = await resolve_download_url(client, create_result)
            if play_url.startswith("http"):
                return {
                    "ok": True,
                    "source": "batch",
                    "download_url": apply_fanqie_vod_query(play_url),
                    "cached": False,
                    "task_id": create_result.get("task_id", ""),
                }
            last_err = "batch_download 未返回下载地址"
        except RuntimeError as exc:
            last_err = str(exc)

        if try_browser:
            from fq_koc_browser import browser_sync_available, sync_koc_auth_via_browser

            if browser_sync_available():
                try:
                    sync = await sync_koc_auth_via_browser(
                        book_id, item_id, open_browser=False
                    )
                    url = str(sync.get("download_url") or "").strip()
                    if url.startswith("http"):
                        return {
                            "ok": True,
                            "source": "browser",
                            "download_url": apply_fanqie_vod_query(url),
                            "cached": False,
                        }
                except Exception as exc:
                    last_err = str(exc)
        break

    return {
        "ok": False,
        "source": "need_capture",
        "message": last_err
        or "在线签名已过期。请在推广中心对该集点「下载」，并启用助手脚本自动回传地址",
        "helper_url": "/koc-capture-helper.js",
    }


async def cache_episode_from_url(
    client: httpx.AsyncClient,
    *,
    book_id: str,
    item_id: str,
    mp4_url: str,
) -> Path:
    """把 CDN 直链下载到本地缓存（生成前可单独调用）。"""
    url = mp4_url.strip()
    if not url.startswith("http"):
        raise RuntimeError("mp4_url 无效")
    cache = _cache_path(book_id, item_id)
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    await download_http_video(
        client,
        url,
        cache,
        referer=koc_referer_url(book_id, item_id),
    )
    validate_local_material(book_id, item_id)
    return cache


async def fetch_fq_koc_episode(
    client: httpx.AsyncClient,
    *,
    book_id: str,
    item_id: str,
    drama_title: str,
    dest_dir: Path,
) -> dict[str, Any]:
    """通过达人中心批量下载接口、CDN 直链或本地缓存获取单集明文 MP4。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", drama_title)[:40] or "fq_koc"
    dest = dest_dir / f"{safe}_{item_id}.mp4"

    cached = find_local_material(book_id, item_id)
    if cached:
        validate_local_material(book_id, item_id)
        _link_or_copy(cached, dest)
        logger.info("使用本地达人中心素材（硬链/拷贝）: %s", cached.name)
        return {
            "local_path": dest,
            "play_url": "",
            "book_id": book_id,
            "item_id": item_id,
            "source": "fq_koc_local",
            "caption": drama_title,
        }

    direct_url = _direct_mp4_url(item_id)
    if direct_url:
        material_path = _cache_path(book_id, item_id)
        if material_path.is_file() and local_material_decode_ok(material_path):
            logger.info("CDN 直链已缓存，跳过重复下载: %s", material_path.name)
        else:
            await download_http_video(
                client,
                direct_url,
                material_path,
                referer=koc_referer_url(book_id, item_id),
            )
            validate_local_material(book_id, item_id)
        _link_or_copy(material_path, dest)
        logger.info("使用 CDN 直链素材: %s", material_path.name)
        return {
            "local_path": dest,
            "play_url": direct_url,
            "book_id": book_id,
            "item_id": item_id,
            "source": "fq_koc_direct",
            "caption": drama_title,
        }

    create_url = _create_url_override()
    _apply_tokens_from_url(create_url)

    if not _cookie_effective():
        raise_material_download_error(book_id=book_id, item_id=item_id)

    if not _ms_token() or not _a_bogus():
        raise_material_download_error(book_id=book_id, item_id=item_id)

    create_result: dict[str, Any] | None = None
    play_url = ""
    last_api_errors: list[str] = []

    try:
        create_result = await create_batch_download(
            client, book_id=book_id, item_id=item_id
        )
        play_url = await resolve_download_url(client, create_result)
    except RuntimeError as exc:
        last_api_errors.append(str(exc))

    if not play_url.startswith("http"):
        from fq_koc_browser import (
            _auto_sync_enabled,
            auto_download_episode_to_cache,
            browser_sync_available,
        )

        if browser_sync_available() and _auto_sync_enabled():
            cache = _cache_path(book_id, item_id)
            logger.info("在线签名失败，改用浏览器自动下载: %s", item_id)
            try:
                await auto_download_episode_to_cache(
                    client,
                    book_id=book_id,
                    item_id=item_id,
                    cache_path=cache,
                )
                validate_local_material(book_id, item_id)
                _link_or_copy(cache, dest)
                return {
                    "local_path": dest,
                    "play_url": "",
                    "book_id": book_id,
                    "item_id": item_id,
                    "source": "fq_koc_auto",
                    "caption": drama_title,
                }
            except RuntimeError as exc:
                last_api_errors.append(str(exc))

        raise_material_download_error(
            book_id=book_id,
            item_id=item_id,
            api_errors=last_api_errors or None,
        )

    cache = _cache_path(book_id, item_id)
    if cache.is_file() and local_material_decode_ok(cache):
        logger.info("命中本地缓存，跳过在线拉片: %s", cache.name)
        _link_or_copy(cache, dest)
    else:
        await download_http_video(
            client, play_url, dest, referer=koc_referer_url(book_id, item_id)
        )
        if not is_usable_video_file(dest):
            raise RuntimeError(
                f"推广中心文件已下载但无法识别为视频（{dest.stat().st_size} 字节），"
                "请重新在推广中心下载或换本地 MP4。"
            )
        _persist_to_cache(dest, book_id, item_id)

    return {
        "local_path": dest,
        "play_url": play_url,
        "book_id": book_id,
        "item_id": item_id,
        "source": "fq_koc",
        "caption": drama_title,
    }
