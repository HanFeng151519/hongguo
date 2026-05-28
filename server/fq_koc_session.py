"""番茄达人中心抓包会话：自动加载/保存，减少反复改 .env。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse

logger = logging.getLogger(__name__)

SESSION_PATH = (
    Path(__file__).resolve().parent.parent
    / "public"
    / "materials"
    / "fq_koc"
    / "koc_session.json"
)

_ENV_MAP = {
    "cookie": "HONGGUO_FQ_KOC_COOKIE",
    "ms_token": "HONGGUO_FQ_KOC_MS_TOKEN",
    "a_bogus": "HONGGUO_FQ_KOC_A_BOGUS",
    "create_url": "HONGGUO_FQ_KOC_CREATE_URL",
    "download_body": "HONGGUO_FQ_KOC_DOWNLOAD_BODY",
}

_RUNTIME_SESSION: dict[str, Any] = {}


def _auto_persist_env() -> bool:
    # 默认不再频繁写 .env，避免运行期 token 抖动导致会话不稳定。
    v = os.getenv("HONGGUO_FQ_KOC_AUTO_PERSIST", "0").strip().lower()
    return v not in ("0", "false", "no", "off")


def _session_mutable() -> bool:
    """生成期间可临时冻结会话 token，避免把已登录态抖掉。"""
    v = os.getenv("HONGGUO_FQ_KOC_SESSION_MUTABLE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _read_session_file() -> dict[str, Any]:
    if not SESSION_PATH.is_file():
        return {}
    try:
        data = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("读取 koc_session.json 失败: %s", exc)
        return {}


def _write_session_file(data: dict[str, Any]) -> None:
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_session_to_env(data: dict[str, Any]) -> None:
    for key, env_key in _ENV_MAP.items():
        val = str(data.get(key) or "").strip()
        if val:
            os.environ[env_key] = val
    body = data.get("last_working_body")
    if isinstance(body, dict) and not os.getenv("HONGGUO_FQ_KOC_DOWNLOAD_BODY", "").strip():
        os.environ["HONGGUO_FQ_KOC_DOWNLOAD_BODY"] = json.dumps(
            body, ensure_ascii=False
        )


def _runtime_session_get() -> dict[str, Any]:
    return dict(_RUNTIME_SESSION) if _RUNTIME_SESSION else {}


def _runtime_session_set(data: dict[str, Any]) -> None:
    _RUNTIME_SESSION.clear()
    _RUNTIME_SESSION.update(data)


def load_koc_session() -> dict[str, Any]:
    """启动时加载上次抓包（覆盖 .env 中同名字段）。"""
    data = _read_session_file()
    if data:
        _runtime_session_set(data)
        apply_session_to_env(data)
        logger.info(
            "已加载达人中心会话 %s（更新于 %s）",
            SESSION_PATH.name,
            data.get("updated_at") or "未知",
        )
    return data


def session_public_status() -> dict[str, Any]:
    data = _runtime_session_get() or _read_session_file()
    return {
        "loaded": bool(data),
        "path": str(SESSION_PATH.relative_to(SESSION_PATH.parent.parent.parent)),
        "updated_at": data.get("updated_at"),
        "has_cookie": bool(str(data.get("cookie") or "").strip()),
        "has_create_url": bool(str(data.get("create_url") or "").strip()),
        "has_download_body": bool(
            str(data.get("download_body") or "").strip()
            or data.get("last_working_body")
        ),
        "has_ms_token": bool(str(data.get("ms_token") or "").strip()),
        "has_a_bogus": bool(str(data.get("a_bogus") or "").strip()),
    }


def save_koc_session(
    *,
    create_url: str = "",
    cookie: str = "",
    ms_token: str = "",
    a_bogus: str = "",
    download_body: str = "",
    last_working_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """合并保存会话，并可选写回 .env 中的 msToken / a_bogus。"""
    data = _runtime_session_get() or _read_session_file()
    old_data = dict(data)
    updates: dict[str, Any] = {}

    mutable = _session_mutable()

    if create_url.strip() and mutable:
        updates["create_url"] = create_url.strip()
        parsed = urlparse(create_url.strip())
        qs = parse_qs(parsed.query)
        if qs.get("msToken"):
            updates["ms_token"] = qs["msToken"][0]
        if qs.get("a_bogus"):
            updates["a_bogus"] = qs["a_bogus"][0]
    if cookie.strip():
        updates["cookie"] = cookie.strip()
    if ms_token.strip() and mutable:
        updates["ms_token"] = ms_token.strip()
    if a_bogus.strip() and mutable:
        updates["a_bogus"] = a_bogus.strip()
    if download_body.strip():
        updates["download_body"] = download_body.strip()

    if last_working_body:
        updates["last_working_body"] = last_working_body

    if not updates:
        return data

    data.update(updates)
    meaningful_keys = (
        "cookie",
        "ms_token",
        "a_bogus",
        "create_url",
        "download_body",
        "last_working_body",
    )
    unchanged = all(old_data.get(k) == data.get(k) for k in meaningful_keys)
    if unchanged:
        _runtime_session_set(data)
        apply_session_to_env(data)
        return data

    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _runtime_session_set(data)
    _write_session_file(data)
    apply_session_to_env(data)

    if _auto_persist_env():
        _patch_env_file(
            {
                k: str(data.get(k) or "")
                for k in ("ms_token", "a_bogus", "create_url")
                if data.get(k)
            }
        )

    return data


def _patch_env_file(updates: dict[str, str]) -> None:
    """仅更新 .env 里达人中心 token 行，不碰 Cookie。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    key_map = {
        "ms_token": "HONGGUO_FQ_KOC_MS_TOKEN",
        "a_bogus": "HONGGUO_FQ_KOC_A_BOGUS",
        "create_url": "HONGGUO_FQ_KOC_CREATE_URL",
    }
    lines = env_path.read_text(encoding="utf-8").splitlines()
    changed = False
    for logical, env_key in key_map.items():
        val = updates.get(logical, "").strip()
        if not val:
            continue
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{env_key}="):
                lines[i] = f"{env_key}={val}"
                found = True
                changed = True
                break
        if not found:
            lines.append(f"{env_key}={val}")
            changed = True
    if changed:
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("已自动写回 .env: %s", ", ".join(key_map.values()))


def parse_f12_curl(text: str) -> dict[str, str]:
    """解析 Chrome「Copy as cURL」文本，提取 URL / Cookie / Payload。"""
    raw = text.strip()
    if not raw:
        raise ValueError("内容为空")

    out: dict[str, str] = {}

    url_m = re.search(
        r"curl\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))",
        raw,
        re.IGNORECASE,
    )
    if url_m:
        out["create_url"] = (url_m.group(1) or url_m.group(2) or url_m.group(3) or "")

    for m in re.finditer(
        r"-H\s+(?:'([^']+)'|\"([^\"]+)\")",
        raw,
        re.IGNORECASE,
    ):
        header = (m.group(1) or m.group(2) or "").strip()
        if ":" not in header:
            continue
        name, _, value = header.partition(":")
        name = name.strip().lower()
        value = value.strip()
        if name == "cookie":
            out["cookie"] = value
        elif name in ("x-ms-token",):
            out["ms_token"] = value

    data_m = re.search(
        r"(?:--data-raw|--data|-d)\s+(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if data_m:
        body = (data_m.group(1) or data_m.group(2) or data_m.group(3) or "").strip()
        if body:
            out["download_body"] = body
            if "=" in body and not body.startswith("{"):
                for key, val in parse_qsl(body, keep_blank_values=True):
                    if key == "msToken":
                        out["ms_token"] = val
                    elif key == "a_bogus":
                        out["a_bogus"] = val

    if out.get("create_url"):
        parsed = urlparse(out["create_url"])
        qs = parse_qs(parsed.query)
        if qs.get("msToken") and "ms_token" not in out:
            out["ms_token"] = qs["msToken"][0]
        if qs.get("a_bogus") and "a_bogus" not in out:
            out["a_bogus"] = qs["a_bogus"][0]

    if not out.get("create_url") and not out.get("cookie"):
        raise ValueError(
            "未能识别 curl：请确认复制的是 batch_download/create 请求的「Copy as cURL」"
        )
    return out


def import_curl_text(text: str) -> dict[str, Any]:
    parsed = parse_f12_curl(text)
    return save_koc_session(
        create_url=parsed.get("create_url", ""),
        cookie=parsed.get("cookie", ""),
        ms_token=parsed.get("ms_token", ""),
        a_bogus=parsed.get("a_bogus", ""),
        download_body=parsed.get("download_body", ""),
    )
