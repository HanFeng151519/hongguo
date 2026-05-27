"""用 Playwright + 系统 Chrome 在达人中心自动拉取明文 MP4。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from fq_koc_material import (
    build_koc_book_detail_url,
    koc_content_hub_url,
    load_book_detail_meta,
    parse_book_detail_from_url,
    save_book_detail_meta,
)
from fq_koc_session import save_koc_session
from material_common import apply_fanqie_vod_query, download_http_video

logger = logging.getLogger(__name__)

KOC_BASE = "https://koc.fqopenplatform.com"
PROFILE_DIR = (
    Path(__file__).resolve().parent.parent
    / "public"
    / "materials"
    / "fq_koc"
    / "pw_profile"
)
LOGIN_MARKER = PROFILE_DIR / ".logged_in"
VOD_URL_RE = re.compile(r"fanqieopenvod|fqkol", re.I)

_BROWSER_LOCK = asyncio.Lock()
_BROWSER_AVAILABLE: Optional[bool] = None
_CREATE_PATH = "/api/platform/content/batch_download/create/v1"

# 常驻 Playwright：首次启动后保持 Chrome 与登录态，避免每集关浏览器
_pw: Any = None
_pw_context: Any = None
_pw_headless: Optional[bool] = None


def _browser_keep_alive() -> bool:
    v = os.getenv("HONGGUO_FQ_KOC_BROWSER_KEEP_ALIVE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _auto_sync_enabled() -> bool:
    """默认关闭；仅在 .env 设 HONGGUO_FQ_KOC_AUTO_SYNC=1 时失败才弹 Playwright。"""
    v = os.getenv("HONGGUO_FQ_KOC_AUTO_SYNC", "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def browser_sync_available() -> bool:
    global _BROWSER_AVAILABLE
    if _BROWSER_AVAILABLE is None:
        try:
            import playwright  # noqa: F401

            _BROWSER_AVAILABLE = True
        except ImportError:
            _BROWSER_AVAILABLE = False
    return _BROWSER_AVAILABLE


def _profile_ready() -> bool:
    return LOGIN_MARKER.is_file() and PROFILE_DIR.is_dir()


def _cookies_header(cookies: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in cookies:
        domain = str(c.get("domain") or "")
        if "fqopenplatform" not in domain and "fanqie" not in domain:
            continue
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _cookie_header_to_playwright(cookie_header: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".fqopenplatform.com",
                "path": "/",
            }
        )
    return out


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
    return ""


def _parse_create_url(url: str) -> tuple[str, str]:
    qs = parse_qs(urlparse(url).query)
    ms = (qs.get("msToken") or [""])[0]
    ab = (qs.get("a_bogus") or [""])[0]
    return ms, ab


async def _wait_for_login(page: Any, *, timeout_sec: int = 180) -> None:
    for _ in range(timeout_sec):
        cookies = await page.context.cookies(KOC_BASE)
        names = {c.get("name") for c in cookies}
        if "sessionid" in names or "sid_tt" in names:
            LOGIN_MARKER.parent.mkdir(parents=True, exist_ok=True)
            LOGIN_MARKER.write_text("1", encoding="utf-8")
            return
        await page.wait_for_timeout(1000)
    raise RuntimeError(
        "浏览器登录超时：请在弹出窗口登录 koc.fqopenplatform.com 后重试"
    )


async def _is_context_alive(ctx: Any) -> bool:
    try:
        if ctx.is_closed():
            return False
        await ctx.cookies()
        return True
    except Exception:
        return False


async def _close_persistent_browser() -> None:
    global _pw, _pw_context, _pw_headless
    if _pw_context is not None:
        try:
            await _pw_context.close()
        except Exception as exc:
            logger.debug("close browser context: %s", exc)
        _pw_context = None
        _pw_headless = None
    if _pw is not None:
        try:
            await _pw.stop()
        except Exception as exc:
            logger.debug("stop playwright: %s", exc)
        _pw = None


async def close_koc_browser() -> None:
    """关闭常驻浏览器（服务退出时可调用）。"""
    async with _BROWSER_LOCK:
        await _close_persistent_browser()


async def _acquire_persistent_context(*, want_headless: bool) -> Any:
    """
    获取常驻 BrowserContext。已存在且仍存活则直接复用（不因 want_headless 重建），
  减少反复关窗导致的登录/签名丢失。
    """
    global _pw, _pw_context, _pw_headless

    if not _browser_keep_alive():
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        ctx = await _launch_context(pw, headless=want_headless)
        ctx._hongguo_ephemeral_pw = pw  # type: ignore[attr-defined]
        return ctx

    async with _BROWSER_LOCK:
        if _pw_context is not None and await _is_context_alive(_pw_context):
            need_visible = not want_headless
            if _pw_headless and need_visible:
                logger.info("需要可见窗口登录，重启 Chrome（原无头实例保留档案）")
                await _close_persistent_browser()
            else:
                logger.debug("复用常驻 Chrome（headless=%s）", _pw_headless)
                return _pw_context

        if _pw_context is not None:
            await _close_persistent_browser()
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        launch_headless = want_headless and _profile_ready()
        _pw_context = await _launch_context(_pw, headless=launch_headless)
        _pw_headless = launch_headless
        logger.info(
            "已启动常驻 Chrome（headless=%s，关闭服务或设 "
            "HONGGUO_FQ_KOC_BROWSER_KEEP_ALIVE=0 可恢复每次关窗）",
            launch_headless,
        )
        return _pw_context


async def _release_context_after_run(context: Any) -> None:
    """单次任务结束：常驻模式只关标签页；非常驻模式关整个浏览器。"""
    if _browser_keep_alive():
        return
    ephemeral = getattr(context, "_hongguo_ephemeral_pw", None)
    try:
        await context.close()
    except Exception:
        pass
    if ephemeral is not None:
        try:
            await ephemeral.stop()
        except Exception:
            pass


async def _launch_context(p: Any, *, headless: bool) -> Any:
    """优先用本机 Chrome/Edge，无需 playwright install chromium。"""
    # 部分网络环境下，系统 DNS 可能会间歇性失败，导致 Playwright 里
    # Chromium 打不开站点（net::ERR_NAME_NOT_RESOLVED）。这里提供 host-resolver-rules 兜底。
    resolver_rules_env = os.getenv(
        "HONGGUO_FQ_KOC_HOST_RESOLVER_RULES", ""
    ).strip()
    host_resolver_rules = ""
    if resolver_rules_env:
        host_resolver_rules = resolver_rules_env
    else:
        fallback_ip = os.getenv(
            "HONGGUO_FQ_KOC_HOST_RESOLVER_KOC_IP", "101.47.85.10"
        ).strip()
        # 如果 DNS 可用，尽量使用实时解析出来的 IP；否则使用 fallback。
        try:
            resolved_ip = socket.gethostbyname("koc.fqopenplatform.com")
            host_resolver_rules = f"MAP koc.fqopenplatform.com {resolved_ip}"
        except Exception:
            if fallback_ip:
                host_resolver_rules = f"MAP koc.fqopenplatform.com {fallback_ip}"

    base_kw: dict[str, Any] = {
        "user_data_dir": str(PROFILE_DIR),
        "headless": headless,
        "viewport": {"width": 1366, "height": 900},
        "locale": "zh-CN",
        "args": ["--disable-blink-features=AutomationControlled"]
        + ([f"--host-resolver-rules={host_resolver_rules}"] if host_resolver_rules else []),
    }
    last_err: Optional[Exception] = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = dict(base_kw)
            if channel:
                kw["channel"] = channel
            return await p.chromium.launch_persistent_context(**kw)
        except Exception as exc:
            last_err = exc
            logger.debug("launch channel=%s failed: %s", channel, exc)
    raise RuntimeError(
        "无法启动浏览器。请安装 Google Chrome，或执行：\n"
        "  pip3 install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
        "  python3 -m playwright install chromium"
    ) from last_err


async def _discover_book_detail_url(
    page: Any,
    book_id: str,
    item_id: str,
    *,
    timeout_ms: int = 60_000,
) -> str:
    """
    在推广中心内容库检索 book_id，从真实跳转后的地址栏解析 genre 等参数并缓存。
    避免手填 HONGGUO_FQ_KOC_GENRE。
    """
    bid = (book_id or "").strip()
    if not bid:
        return build_koc_book_detail_url(book_id, item_id)

    cached = load_book_detail_meta(bid)
    if cached.get("genre"):
        url = build_koc_book_detail_url(bid, item_id)
        logger.info(
            "复用已检索的 book-detail（genre=%s）",
            cached.get("genre"),
        )
        return url

    hub = koc_content_hub_url()
    logger.info("浏览器检索 book_id=%s：打开内容库", bid)
    await page.goto(hub, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(2500)

    navigated = await page.evaluate(
        """async (bookId) => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const hasBook = (href) =>
            href && (href.includes('book_id=' + bookId) || href.includes(bookId));
          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            if (hasBook(href)) {
              a.click();
              return { via: 'link', href: a.href || href };
            }
          }
          const inputs = [...document.querySelectorAll('input')].filter((el) => {
            const hint =
              (el.placeholder || '') +
              (el.getAttribute('aria-label') || '') +
              (el.className || '');
            return /搜|查询|关键|search/i.test(hint) || el.type === 'search';
          });
          if (inputs.length) {
            const inp = inputs[0];
            inp.focus();
            inp.value = bookId;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(
              new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
            );
            await sleep(2500);
            for (const a of document.querySelectorAll('a[href]')) {
              const href = a.getAttribute('href') || '';
              if (hasBook(href)) {
                a.click();
                return { via: 'search', href: a.href || href };
              }
            }
          }
          return { via: 'none' };
        }""",
        bid,
    )
    logger.info("内容库检索结果: %s", navigated.get("via"))

    if navigated.get("via") != "none":
        try:
            await page.wait_for_url("**/book-detail**", timeout=15_000)
        except Exception:
            await page.wait_for_timeout(3000)
    else:
        seed = build_koc_book_detail_url(bid, item_id)
        logger.info("内容库未命中链接，尝试直达: %s", seed[:100])
        await page.goto(seed, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3000)

    final = page.url or ""
    if "book-detail" in final and bid in final:
        meta = parse_book_detail_from_url(final)
        if meta.get("genre"):
            save_book_detail_meta(bid, meta)
        if item_id and f"item_id={item_id}" not in final:
            target = build_koc_book_detail_url(bid, item_id)
            if target != final:
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=timeout_ms
                )
            return target
        return final

    return build_koc_book_detail_url(bid, item_id)


async def _run_in_browser(
    book_id: str,
    item_id: str,
    *,
    open_browser: Optional[bool] = None,
    timeout_sec: int = 180,
    download_to: Optional[Path] = None,
) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "自动下载需要 Playwright（仅 Python 包，约几 MB）：\n"
            "  pip3 install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple\n"
            "使用本机 Chrome 时不必下载 Chromium。"
        ) from exc

    if open_browser is True:
        want_headless = False
    elif open_browser is False:
        want_headless = True
    else:
        want_headless = _profile_ready()

    captured: dict[str, Any] = {}
    vod_urls: list[str] = []
    evaluate_result: dict[str, Any] = {}

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    context = await _acquire_persistent_context(want_headless=want_headless)
    page: Any = None
    try:
        page = await context.new_page()

        env_cookie = os.getenv("HONGGUO_FQ_KOC_COOKIE", "").strip()
        if env_cookie:
            try:
                await context.add_cookies(
                    _cookie_header_to_playwright(env_cookie)
                )
            except Exception as exc:
                logger.debug("inject env cookie: %s", exc)

        page_url = await _discover_book_detail_url(
            page,
            book_id,
            item_id,
            timeout_ms=timeout_sec * 1000,
        )
        logger.info("Playwright 使用 book-detail: %s", page_url)

        def on_request(request: Any) -> None:
            if (
                _CREATE_PATH in request.url
                and request.method.upper() == "POST"
            ):
                captured["create_url"] = request.url
                captured["post_data"] = request.post_data or ""

        async def on_response(response: Any) -> None:
            url = response.url
            if _CREATE_PATH in url:
                captured["response_status"] = response.status
                try:
                    captured["response_body"] = await response.text()
                except Exception:
                    pass
            if VOD_URL_RE.search(url) and response.status in (200, 206):
                ct = (response.headers.get("content-type") or "").lower()
                if "video" in ct or "mp4" in url or "octet" in ct:
                    vod_urls.append(url)

        page.on("request", on_request)
        page.on("response", on_response)

        if "book-detail" not in (page.url or "") or book_id not in (page.url or ""):
            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=timeout_sec * 1000,
            )
            await page.wait_for_timeout(2500)
            meta = parse_book_detail_from_url(page.url or "")
            if meta.get("genre"):
                save_book_detail_meta(book_id, meta)

        cookies = await context.cookies(KOC_BASE)
        names = {c.get("name") for c in cookies}
        if "sessionid" in names or "sid_tt" in names:
            LOGIN_MARKER.parent.mkdir(parents=True, exist_ok=True)
            LOGIN_MARKER.write_text("1", encoding="utf-8")
        else:
            await _wait_for_login(page, timeout_sec=min(180, timeout_sec))

        evaluate_result = await page.evaluate(
            """async ({bookId, itemId}) => {
              const path = '/api/platform/content/batch_download/create/v1';
              const body = new URLSearchParams({
                book_id: bookId,
                item_id: itemId,
                content_tab: '6',
                app_id: '457699',
                aid: '457699',
                origin_app_id: '457699',
                host_app_id: '457699',
              });
              const res = await fetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: body.toString(),
                credentials: 'include',
              });
              const text = await res.text();
              return { status: res.status, url: res.url, text, ok: res.ok };
            }""",
            {"bookId": book_id, "itemId": item_id},
        )
        await page.wait_for_timeout(2000)

        cookie_header = _cookies_header(await context.cookies(KOC_BASE))
        captured["_cookie_header"] = cookie_header

        create_url = str(
            captured.get("create_url") or evaluate_result.get("url") or ""
        ).strip()
        post_data = str(captured.get("post_data") or "").strip()
        resp_text = str(
            captured.get("response_body") or evaluate_result.get("text") or ""
        ).strip()

        download_url = ""
        if resp_text:
            try:
                download_url = _extract_download_url(json.loads(resp_text))
            except json.JSONDecodeError:
                pass
        if not download_url and vod_urls:
            download_url = vod_urls[-1]

        if not download_url:
            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            try:
                await page.get_by_text("下载", exact=False).first.click(
                    timeout=5_000
                )
                click_note = "clicked_playwright_text"
            except Exception:
                click_note = await page.evaluate(
                    """({itemId}) => {
                      const id = String(itemId);
                      const isVisible = (el) => el && el.offsetParent !== null;
                      const isDl = (el) => {
                        const t = ((el.textContent || '') + (el.getAttribute('aria-label') || '')).trim();
                        return /下载|download|导出/i.test(t);
                      };
                      for (const el of document.querySelectorAll(
                        'button, a, [role="button"], [class*="download"], [class*="Download"]'
                      )) {
                        if (isVisible(el) && isDl(el)) {
                          el.click();
                          return 'clicked_detail:' + (el.textContent || '').trim().slice(0, 16);
                        }
                      }
                      const rows = document.querySelectorAll(
                        'tr, [class*="row"], [class*="item"], [class*="card"]'
                      );
                      for (const row of rows) {
                        const html = row.innerHTML || '';
                        if (!html.includes(id)) continue;
                        for (const el of row.querySelectorAll('button, a, span, div')) {
                          if (isVisible(el) && isDl(el)) {
                            el.click();
                            return 'clicked_row';
                          }
                        }
                      }
                      for (const el of document.querySelectorAll('button, a, span')) {
                        if (isVisible(el) && isDl(el)) {
                          el.click();
                          return 'clicked_any';
                        }
                      }
                      return 'not_found';
                    }""",
                    {"itemId": item_id},
                )
            logger.info("推广中心页面点击下载: %s", click_note)
            if click_note == "not_found" and want_headless:
                wait_sec = 5
            elif str(click_note).startswith("clicked"):
                wait_sec = 45
            else:
                # 推广中心有时会延迟返回 CDN 地址；之前默认只等 20s
                # 容易导致仍在等待时就返回 400。
                wait_sec = int(
                    os.getenv("HONGGUO_FQ_KOC_SYNC_WAIT_SEC", "60").strip() or "60"
                )
            logger.info(
                "接口未返回地址，等待浏览器产生 MP4 请求（最多 %s 秒）…",
                wait_sec,
            )
            for i in range(wait_sec):
                if vod_urls:
                    download_url = vod_urls[-1]
                    logger.info("已捕获 CDN 地址")
                    break
                if i > 0 and i % 5 == 0:
                    logger.info("仍在等待 MP4… %s/%s 秒", i, wait_sec)
                await page.wait_for_timeout(1000)

        if not download_url:
            raise RuntimeError(
                "未捕获到 MP4 地址（请在弹出的 Chrome 里对该集点「下载」，"
                "或确认推广中心已登录且有权限）"
            )

        mp4_url = apply_fanqie_vod_query(download_url)

        if download_to is not None:
            logger.info("正在从 CDN 拉取 MP4 到本地（体积大时可能需数分钟）…")
            download_to.parent.mkdir(parents=True, exist_ok=True)
            resp = await context.request.get(
                mp4_url,
                headers={"Referer": page_url},
                timeout=1_800_000,
            )
            if resp.status not in (200, 206):
                raise RuntimeError(f"CDN 下载失败 HTTP {resp.status}")
            body = await resp.body()
            if len(body) < 100_000:
                raise RuntimeError("下载文件过小，可能链接已过期")
            download_to.write_bytes(body)

        if create_url:
            ms_token, a_bogus = _parse_create_url(create_url)
            save_koc_session(
                create_url=create_url,
                cookie=cookie_header or os.getenv("HONGGUO_FQ_KOC_COOKIE", ""),
                ms_token=ms_token,
                a_bogus=a_bogus,
                download_body=post_data
                or (
                    f"book_id={book_id}&item_id={item_id}&content_tab=6"
                    "&app_id=457699&aid=457699&origin_app_id=457699"
                    "&host_app_id=457699"
                ),
                last_working_body={
                    "book_id": book_id,
                    "item_id": item_id,
                    "content_tab": "6",
                    "app_id": "457699",
                    "aid": "457699",
                    "origin_app_id": "457699",
                    "host_app_id": "457699",
                },
            )

        return {
            "download_url": mp4_url,
            "create_url": create_url,
            "cookie_header": cookie_header,
        }
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception as exc:
                logger.debug("close page: %s", exc)
        await _release_context_after_run(context)


async def auto_download_episode_to_cache(
    client: httpx.AsyncClient,
    *,
    book_id: str,
    item_id: str,
    cache_path: Path,
    open_browser: Optional[bool] = None,
) -> Path:
    """自动在浏览器内签名并下载 MP4 到本地缓存路径。"""
    if not _auto_sync_enabled():
        raise RuntimeError("自动下载已关闭（HONGGUO_FQ_KOC_AUTO_SYNC=0）")

    # 已登录（pw_profile + .logged_in）时默认无头后台拉片，避免每集都弹窗
    if open_browser is True:
        headed_tries = [True]
    elif open_browser is False:
        headed_tries = [False]
    elif _profile_ready():
        headed_tries = [False, True]
    else:
        headed_tries = [True]
    last_err: Optional[Exception] = None
    info: Optional[dict[str, Any]] = None
    for try_headed in headed_tries:
        try:
            info = await _run_in_browser(
                book_id,
                item_id,
                open_browser=try_headed,
                timeout_sec=180,
                download_to=cache_path,
            )
            break
        except RuntimeError as exc:
            last_err = exc
            msg = str(exc)
            if try_headed is False and (
                "未捕获到 MP4" in msg or "CDN 下载失败" in msg
            ):
                logger.info("无头模式未拿到成片，改用可见 Chrome 重试…")
                continue
            if "登录" not in msg and "未登录" not in msg:
                raise
    if info is None:
        raise last_err or RuntimeError("浏览器自动下载失败")

    logger.info(
        "浏览器自动下载完成 %s (%.1f MB)",
        cache_path.name,
        cache_path.stat().st_size / (1024 * 1024),
    )
    return cache_path


async def sync_koc_auth_via_browser(
    book_id: str,
    item_id: str,
    *,
    open_browser: Optional[bool] = None,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    info = await _run_in_browser(
        book_id, item_id, open_browser=open_browser, timeout_sec=timeout_sec
    )
    return {
        "ok": True,
        "create_url": info.get("create_url", ""),
        "download_url": info.get("download_url", ""),
        "has_ms_token": bool(info.get("create_url")),
        "has_a_bogus": bool(info.get("create_url")),
        "has_cookie": bool(info.get("cookie_header")),
    }


async def ensure_koc_auth(
    book_id: str,
    item_id: str,
    *,
    force: bool = False,
) -> bool:
    if not _auto_sync_enabled() or not browser_sync_available():
        return False
    from fq_koc_material import _a_bogus, _cookie_effective, _ms_token

    if not force and _cookie_effective() and _ms_token() and _a_bogus():
        return False
    try:
        # 有浏览器档案时优先无头同步 token，仅失败时再弹窗（见 sync 内重试）
        ob: Optional[bool] = False if _profile_ready() else None
        await sync_koc_auth_via_browser(book_id, item_id, open_browser=ob)
        return True
    except Exception as exc:
        logger.warning("自动同步达人中心权限失败: %s", exc)
        return False
