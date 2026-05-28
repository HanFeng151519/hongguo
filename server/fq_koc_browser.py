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
_pw_page: Any = None


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


def _stable_member_content_url() -> str:
    """避免 invite 链接反复触发机构认证，优先走稳定会员页。"""
    return f"{KOC_BASE}/page/member/content"


def _login_entry_url(book_id: str = "") -> str:
    """
    登录入口优先机构邀请页（可显示机构名，如“不鸣文化”）；
    若 invite 未配置则退回稳定会员页。
    """
    try:
        return koc_content_hub_url(book_id)
    except Exception:
        return _stable_member_content_url()


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


class KocLoginRequiredError(RuntimeError):
    """用户尚未完成达人中心登录。"""


def _login_wait_sec(default_sec: int = 600) -> int:
    raw = os.getenv("HONGGUO_FQ_KOC_LOGIN_WAIT_SEC", "").strip()
    try:
        sec = int(raw) if raw else default_sec
    except ValueError:
        sec = default_sec
    return max(60, sec)


async def _wait_for_login(page: Any, *, timeout_sec: int = 600) -> None:
    for _ in range(timeout_sec):
        cookies = await page.context.cookies(KOC_BASE)
        names = {c.get("name") for c in cookies}
        if "sessionid" in names or "sid_tt" in names:
            LOGIN_MARKER.parent.mkdir(parents=True, exist_ok=True)
            LOGIN_MARKER.write_text("1", encoding="utf-8")
            return
        await page.wait_for_timeout(1000)
    raise KocLoginRequiredError(
        "请先完成登录：浏览器登录超时，未检测到 sessionid/sid_tt；"
        "登录完成后再继续自动下载。"
    )


async def ensure_koc_login(
    book_id: str,
    *,
    timeout_sec: Optional[int] = None,
) -> dict[str, Any]:
    """仅执行登录同步，不触发任何下载。"""
    global _pw_page
    wait_sec = timeout_sec if timeout_sec is not None else _login_wait_sec()
    context = await _acquire_persistent_context(want_headless=False)
    page: Any = None
    keep_page_open = False
    try:
        reused = None
        if _browser_keep_alive():
            reused = _pw_page if await _is_page_alive(_pw_page) else None
            if reused is None:
                reused = await _pick_reusable_page(context)
        if reused is not None:
            page = reused
            logger.info("登录同步：复用现有浏览器标签页")
        else:
            page = _pw_page
            page = await context.new_page()
            logger.info("登录同步：新建浏览器标签页")
        try:
            await page.goto(
                _login_entry_url(book_id),
                wait_until="domcontentloaded",
                timeout=120_000,
            )
        except Exception:
            await page.goto(
                f"{KOC_BASE}/page/member/content",
                wait_until="domcontentloaded",
                timeout=120_000,
            )
        await _wait_for_login(page, timeout_sec=wait_sec)
        logger.info("达人中心登录完成，后续将复用同一会话")
        if _browser_keep_alive():
            _pw_page = page
            keep_page_open = True
        return {"ok": True, "wait_sec": wait_sec, "logged_in": True}
    finally:
        if page is not None and not keep_page_open:
            try:
                await page.close()
            except Exception:
                pass
        await _release_context_after_run(context)


async def _is_context_alive(ctx: Any) -> bool:
    try:
        if ctx.is_closed():
            return False
        await ctx.cookies()
        return True
    except Exception:
        return False


async def _close_persistent_browser() -> None:
    global _pw, _pw_context, _pw_headless, _pw_page
    _pw_page = None
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


async def _is_page_alive(page: Any) -> bool:
    try:
        if page is None or page.is_closed():
            return False
        _ = page.url
        return True
    except Exception:
        return False


async def _pick_reusable_page(context: Any) -> Any:
    """
    优先复用已存在标签页，避免每次任务新开画面。
    选择顺序：最后一个存活页 -> 任意存活页 -> None。
    """
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    for p in reversed(pages):
        if await _is_page_alive(p):
            return p
    return None


async def _is_login_gate_page(page: Any) -> bool:
    """判断当前是否仍在机构登录页。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "login" in url:
        return True
    try:
        return bool(
            await page.evaluate(
                """() => {
                  const txt = document.body?.innerText || '';
                  if (/机构成员登录|验证码登录|登录\\/注册/.test(txt)) return true;
                  const telInput = document.querySelector(
                    "input[placeholder*='手机号'], input[placeholder*='验证码']"
                  );
                  return !!telInput;
                }"""
            )
        )
    except Exception:
        return False


async def _has_login_cookie(context: Any) -> bool:
    try:
        cookies = await context.cookies(KOC_BASE)
        names = {c.get("name") for c in cookies}
        return "sessionid" in names or "sid_tt" in names
    except Exception:
        return False


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
            # 核心策略：只要常驻上下文活着，就永远复用，不因 headless 诉求重建。
            # 否则会触发反复重启 -> 反复登录。
            logger.debug("复用常驻 Chrome（headless=%s）", _pw_headless)
            return _pw_context

        if _pw_context is not None:
            await _close_persistent_browser()
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        # 首次优先有头，确保用户可见登录一次；后续都复用同一上下文。
        launch_headless = bool(
            want_headless
            and _profile_ready()
            and os.getenv("HONGGUO_FQ_KOC_FIRST_HEADED", "1").strip().lower()
            in ("0", "false", "no", "off")
        )
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
    drama_title: str = "",
    *,
    timeout_ms: int = 60_000,
) -> str:
    """
    先进入推广中心内容库主页，再尝试检索 book_id 并解析 book-detail 参数缓存。
    默认不强制直达 book-detail，避免落在“加载中”页。
    """
    bid = (book_id or "").strip()
    keyword = (drama_title or "").strip()
    if not bid:
        return build_koc_book_detail_url(book_id, item_id)

    hub = _stable_member_content_url()
    cur_url = (page.url or "").strip()
    can_reuse_current = (
        "fqopenplatform.com" in cur_url
        and "page/member/content" in cur_url
        and not await _is_login_gate_page(page)
    )
    if can_reuse_current:
        logger.info("浏览器检索 book_id=%s：复用页直检索（未重新跳转）", bid)
    else:
        logger.info("浏览器检索 book_id=%s：打开内容库", bid)
        await page.goto(hub, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3500)
        if await _is_login_gate_page(page):
            raise KocLoginRequiredError(
                "请先完成登录：当前仍在机构成员登录页，未进入内容库。"
            )

    async def _click_first_result(*, term: str) -> dict[str, str]:
        """优先点包含剧名的结果链接；其次点包含 book_id 的链接。"""
        t = (term or "").strip()
        if t:
            try:
                loc = page.locator(f"a:has-text('{t}')").first
                if await loc.count():
                    await loc.click(timeout=2500)
                    return {"via": "pw_link_title", "term": t}
            except Exception:
                pass
        try:
            loc = page.locator(f"a[href*='{bid}']").first
            if await loc.count():
                await loc.click(timeout=2500)
                return {"via": "pw_link_book_id", "term": bid}
        except Exception:
            pass
        return {"via": "no_result_link"}

    async def _search_once(term: str) -> dict[str, str]:
        term = (term or "").strip()
        if not term:
            return {"via": "empty_term"}
        selectors = (
            "input[placeholder*='Book']",
            "input[placeholder*='book']",
            "input[placeholder*='书名']",
            "input[placeholder*='作者']",
            "input[type='search']",
            "input.el-input__inner",
        )
        input_loc = None
        for sel in selectors:
            loc = page.locator(sel)
            if await loc.count():
                input_loc = loc.first
                break
        if input_loc is None:
            return {"via": "fallback_no_input", "term": term}
        try:
            await input_loc.click(timeout=2000)
            await input_loc.fill("")
            await input_loc.fill(term)
            await input_loc.press("Enter")
            await page.wait_for_timeout(2200)
        except Exception:
            return {"via": "input_action_failed", "term": term}
        hit = await _click_first_result(term=term)
        if hit.get("via", "").startswith("pw_link_"):
            return hit
        return {"via": "searched_no_hit", "term": term}

    # 先用 Playwright 原生输入事件做一轮搜索（比页面内 evaluate 触发更稳）。
    title_variants: list[str] = []
    if keyword:
        title_variants.append(keyword)
        short = re.split(r"[：:|·\\-\\s]", keyword)[0].strip()
        if short and short != keyword:
            title_variants.append(short)
    title_variants.append(bid)
    seen_terms: set[str] = set()
    for q in title_variants:
        q = q.strip()
        if not q or q in seen_terms:
            continue
        seen_terms.add(q)
        pw_try = await _search_once(q)
        logger.info("内容库原生检索结果: %s（keyword=%s）", pw_try.get("via"), q[:30])
        if pw_try.get("via", "").startswith("pw_link_"):
            try:
                await page.wait_for_url("**/book-detail**", timeout=12_000)
            except Exception:
                await page.wait_for_timeout(2500)
            break

    navigated = await page.evaluate(
        """async ({ bookId, keyword }) => {
          const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
          const hasBook = (href) =>
            href && (href.includes('book_id=' + bookId) || href.includes(bookId));
          const hasKeyword = (text) =>
            keyword && text && String(text).includes(keyword);
          const searchText = keyword || bookId;
          const inputSelectors = [
            'input[placeholder*="Book"]',
            'input[placeholder*="book"]',
            'input[placeholder*="书名"]',
            'input[placeholder*="作者"]',
            'input[type="search"]',
            'input.el-input__inner',
            'input',
          ];
          const pickInput = () => {
            const isVisible = (el) =>
              !!el &&
              !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            for (const sel of inputSelectors) {
              const list = [...document.querySelectorAll(sel)];
              // 优先右上搜索框（通常宽度更大、位置更靠右）
              list.sort((a, b) => {
                const ra = a.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                return rb.right - ra.right;
              });
              const hit = list.find(isVisible);
              if (hit) return hit;
            }
            return null;
          };
          const clickResultByText = () => {
            const rows = [
              ...document.querySelectorAll(
                'a[href], .item, .card, .row, li, tr, [class*="item"], [class*="card"]'
              ),
            ];
            for (const el of rows) {
              const txt = (el.textContent || '').trim();
              if (!txt) continue;
              const href = el.getAttribute && (el.getAttribute('href') || '');
              if (hasBook(href)) {
                el.click();
                return { via: 'row_href', href: href || '' };
              }
              if (hasKeyword(txt)) {
                // 优先点击行内可跳转 a
                const a = el.querySelector && el.querySelector('a[href]');
                if (a) {
                  a.click();
                  return { via: 'row_title_link', href: a.getAttribute('href') || '' };
                }
                el.click();
                return { via: 'row_title', href: href || '' };
              }
            }
            return null;
          };
          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            if (hasBook(href)) {
              a.click();
              return { via: 'link', href: a.href || href };
            }
            if (hasKeyword(a.textContent || '')) {
              a.click();
              return { via: 'link_title', href: a.href || href };
            }
          }
          const inp = pickInput();
          if (inp) {
            inp.focus();
            inp.value = searchText;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            inp.dispatchEvent(new Event('change', { bubbles: true }));
            inp.dispatchEvent(
              new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
            );
            inp.dispatchEvent(
              new KeyboardEvent('keyup', { key: 'Enter', bubbles: true })
            );
            await sleep(2500);
            const rowHit = clickResultByText();
            if (rowHit) return rowHit;
            for (const a of document.querySelectorAll('a[href]')) {
              const href = a.getAttribute('href') || '';
              if (hasBook(href)) {
                a.click();
                return { via: 'search', href: a.href || href };
              }
              if (hasKeyword(a.textContent || '')) {
                a.click();
                return { via: 'search_title', href: a.href || href };
              }
            }
            return { via: 'searched_no_hit', q: searchText };
          }
          return { via: 'none', q: searchText };
        }""",
        {"bookId": bid, "keyword": keyword},
    )
    logger.info(
        "内容库检索结果: %s（keyword=%s）",
        navigated.get("via"),
        (keyword or bid)[:30],
    )

    if navigated.get("via") in ("none", "searched_no_hit") and keyword and keyword != bid:
        await page.wait_for_timeout(1800)
        retry = await page.evaluate(
            """async ({ bookId }) => {
              const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
              const hasBook = (href) =>
                href && (href.includes('book_id=' + bookId) || href.includes(bookId));
              const inputs = [
                ...document.querySelectorAll(
                  'input[placeholder*="Book"], input[placeholder*="book"], input[placeholder*="书名"], input[type="search"], input.el-input__inner, input'
                ),
              ].filter((el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length));
              if (!inputs.length) return { via: 'fallback_no_input' };
              inputs.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right);
              const inp = inputs[0];
              inp.focus();
              inp.value = bookId;
              inp.dispatchEvent(new Event('input', { bubbles: true }));
              inp.dispatchEvent(new Event('change', { bubbles: true }));
              inp.dispatchEvent(
                new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
              );
              inp.dispatchEvent(
                new KeyboardEvent('keyup', { key: 'Enter', bubbles: true })
              );
              await sleep(2500);
              for (const a of document.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                if (hasBook(href)) {
                  a.click();
                  return { via: 'search_book_id', href: a.href || href };
                }
              }
              return { via: 'fallback_no_hit' };
            }""",
            {"bookId": bid},
        )
        logger.info("内容库检索回退 book_id 结果: %s", retry.get("via"))
        if retry.get("via") in ("search_book_id",):
            try:
                await page.wait_for_url("**/book-detail**", timeout=15_000)
            except Exception:
                await page.wait_for_timeout(2500)

    if navigated.get("via") != "none":
        try:
            await page.wait_for_url("**/book-detail**", timeout=15_000)
        except Exception:
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

    logger.info("内容库未命中 book-detail，保持主页检索流程")
    return hub


async def _run_in_browser(
    book_id: str,
    item_id: str,
    drama_title: str = "",
    *,
    open_browser: Optional[bool] = None,
    timeout_sec: int = 180,
    download_to: Optional[Path] = None,
) -> dict[str, Any]:
    global _pw_page
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
    reused_existing_page = False
    original_page_url = ""
    no_navigate_mode = (
        os.getenv("HONGGUO_FQ_KOC_NO_NAVIGATE", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    manual_click_only = (
        os.getenv("HONGGUO_FQ_KOC_MANUAL_CLICK_ONLY", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )
    try:
        reused = None
        if _browser_keep_alive():
            reused = _pw_page if await _is_page_alive(_pw_page) else None
            if reused is None:
                reused = await _pick_reusable_page(context)
        if reused is not None:
            page = reused
            reused_existing_page = True
            original_page_url = (page.url or "").strip()
            logger.info("下载链路：复用现有浏览器标签页")
        else:
            page = await context.new_page()
            logger.info("下载链路：新建浏览器标签页")

        env_cookie = os.getenv("HONGGUO_FQ_KOC_COOKIE", "").strip()
        if env_cookie:
            try:
                await context.add_cookies(
                    _cookie_header_to_playwright(env_cookie)
                )
            except Exception as exc:
                logger.debug("inject env cookie: %s", exc)

        # 先确保登录，再做内容检索；避免在“机构成员登录”页误把手机号输入框当搜索框。
        cookies = await context.cookies(KOC_BASE)
        names = {c.get("name") for c in cookies}
        logged_in = "sessionid" in names or "sid_tt" in names
        if not logged_in:
            if want_headless:
                raise KocLoginRequiredError(
                    "请先完成登录：当前为无头模式且未检测到登录态，"
                    "请先点“仅登录同步”完成登录后再生成。"
                )
            try:
                await page.goto(
                    _login_entry_url(book_id),
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
            except Exception:
                await page.goto(
                    f"{KOC_BASE}/page/member/content",
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
            await _wait_for_login(
                page, timeout_sec=max(timeout_sec, _login_wait_sec())
            )

        page_url = (page.url or "").strip() or _stable_member_content_url()
        if await _is_login_gate_page(page):
            raise KocLoginRequiredError("请先完成登录：当前页面仍是机构成员登录页。")
        if no_navigate_mode:
            logger.info("下载链路：无跳转模式（复用当前页，仅发接口请求）")
        else:
            page_url = await _discover_book_detail_url(
                page,
                book_id,
                item_id,
                drama_title=drama_title,
                timeout_ms=timeout_sec * 1000,
            )
            if "book-detail" not in (page_url or ""):
                raise RuntimeError("内容库未命中目标剧，跳过浏览器自动下载")
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

        if (not no_navigate_mode) and (
            "book-detail" not in (page.url or "") or book_id not in (page.url or "")
        ):
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
            await _wait_for_login(
                page, timeout_sec=max(timeout_sec, _login_wait_sec())
            )

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

        if not download_url and no_navigate_mode and manual_click_only:
            wait_sec = int(
                os.getenv("HONGGUO_FQ_KOC_MANUAL_WAIT_SEC", "90").strip() or "90"
            )
            logger.info(
                "无跳转手动模式：请在当前达人页面手动点目标集下载（等待 %s 秒）",
                wait_sec,
            )
            for _ in range(wait_sec):
                if (not await _has_login_cookie(context)) or await _is_login_gate_page(page):
                    raise KocLoginRequiredError("手动模式等待期间登录态丢失，请重新登录。")
                if vod_urls:
                    download_url = vod_urls[-1]
                    break
                await page.wait_for_timeout(1000)
            if not download_url:
                raise RuntimeError(
                    "手动模式未捕获到下载地址：请在当前达人页面手动搜索并点击该集下载后重试。"
                )

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
                    os.getenv("HONGGUO_FQ_KOC_SYNC_WAIT_SEC", "120").strip() or "120"
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
        if (
            _browser_keep_alive()
            and reused_existing_page
            and page is not None
            and await _is_page_alive(page)
            and original_page_url
            and (page.url or "").strip() != original_page_url
        ):
            try:
                logger.info("下载链路：恢复复用页到原地址")
                await page.goto(
                    original_page_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except Exception as exc:
                logger.debug("恢复复用页失败: %s", exc)
        if _browser_keep_alive() and page is not None and await _is_page_alive(page):
            _pw_page = page
        if page is not None and not _browser_keep_alive():
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
    drama_title: str = "",
    open_browser: Optional[bool] = None,
) -> Path:
    """自动在浏览器内签名并下载 MP4 到本地缓存路径。"""
    if not _auto_sync_enabled():
        raise RuntimeError("自动下载已关闭（HONGGUO_FQ_KOC_AUTO_SYNC=0）")

    async def _ensure_first_login_if_needed() -> None:
        """首次无登录标记时，强制有头登录一次并持久化到 pw_profile。"""
        if _profile_ready():
            return
        logger.info("首次使用达人中心，先打开浏览器完成登录...")
        await ensure_koc_login(book_id, timeout_sec=_login_wait_sec())

    await _ensure_first_login_if_needed()

    # 单会话策略：不再无头/有头来回切换重试，避免触发上下文重建与掉登录态。
    try_open = (
        True if open_browser is None else bool(open_browser)
    )
    info = await _run_in_browser(
        book_id,
        item_id,
        drama_title=drama_title,
        open_browser=try_open,
        timeout_sec=180,
        download_to=cache_path,
    )

    logger.info(
        "浏览器自动下载完成 %s (%.1f MB)",
        cache_path.name,
        cache_path.stat().st_size / (1024 * 1024),
    )
    return cache_path


async def sync_koc_auth_via_browser(
    book_id: str,
    item_id: str,
    drama_title: str = "",
    *,
    open_browser: Optional[bool] = None,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    info = await _run_in_browser(
        book_id,
        item_id,
        drama_title=drama_title,
        open_browser=open_browser,
        timeout_sec=timeout_sec,
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
    drama_title: str = "",
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
        await sync_koc_auth_via_browser(
            book_id, item_id, drama_title=drama_title, open_browser=ob
        )
        return True
    except Exception as exc:
        logger.warning("自动同步达人中心权限失败: %s", exc)
        return False
