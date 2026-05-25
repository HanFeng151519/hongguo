"""一键剪辑用 LLM 客户端（OpenAI 兼容：LM Studio / DashScope 等）。"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

LM_STUDIO_DEFAULT_BASE = "http://127.0.0.1:1234/v1"
LM_STUDIO_DEFAULT_MODEL = "gemma-4-e4b-it"
LM_STUDIO_PLACEHOLDER_KEY = "lm-studio"


def api_key() -> str:
    return (
        os.getenv("QWEN_API_KEY", "").strip()
        or os.getenv("DASHSCOPE_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )


def api_base() -> str:
    return (
        os.getenv("QWEN_API_BASE", "").strip()
        or os.getenv("LLM_API_BASE", "").strip()
        or LM_STUDIO_DEFAULT_BASE
    ).rstrip("/")


def default_model() -> str:
    return (
        os.getenv("QWEN_MODEL", "").strip()
        or os.getenv("LLM_MODEL", "").strip()
        or LM_STUDIO_DEFAULT_MODEL
    )


def code_model() -> str:
    return (
        os.getenv("QWEN_CODE_MODEL", "").strip()
        or os.getenv("LLM_CODE_MODEL", "").strip()
        or default_model()
    )


def temperature() -> float:
    try:
        return float(os.getenv("QWEN_TEMPERATURE", os.getenv("LLM_TEMPERATURE", "0")))
    except ValueError:
        return 0.0


def is_lm_studio() -> bool:
    host = (urlparse(api_base()).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def llm_provider_label() -> str:
    return "LM Studio" if is_lm_studio() else "云端 API"


def is_configured() -> bool:
    """LM Studio 本地无需真实 Key；云端需配置 Key。"""
    if is_lm_studio():
        return True
    return bool(api_key())


def _auth_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = api_key() or (LM_STUDIO_PLACEHOLDER_KEY if is_lm_studio() else "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def format_api_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        try:
            body = exc.response.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if body.get("message"):
                return str(body["message"])
            if body.get("code"):
                return f"{body['code']}: {body.get('message') or ''}".strip(": ")
    text = str(exc)
    if is_lm_studio() and ("connect" in text.lower() or "connection" in text.lower()):
        return (
            "无法连接 LM Studio，请确认已启动并在 Local Server 开启 "
            f"（默认 {LM_STUDIO_DEFAULT_BASE}）"
        )
    if "blocked" in text.lower():
        return "API Key 在 DashScope 侧被禁用，请在控制台检查密钥状态或新建密钥"
    return text


async def chat_completion(
    client: httpx.AsyncClient,
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    json_mode: bool = True,
    temperature_override: Optional[float] = None,
) -> str:
    if not is_configured():
        raise RuntimeError(
            "未配置 LLM API Key（本地 LM Studio 请设置 QWEN_API_BASE；"
            "云端请设置 QWEN_API_KEY）"
        )

    payload: dict[str, Any] = {
        "model": model or default_model(),
        "messages": messages,
        "temperature": (
            temperature_override
            if temperature_override is not None
            else temperature()
        ),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = 300.0 if is_lm_studio() else 120.0
    url = f"{api_base()}/chat/completions"
    headers = _auth_headers()

    resp = await client.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code == 400 and json_mode and "response_format" in payload:
        payload = {k: v for k, v in payload.items() if k != "response_format"}
        logger.info("当前模型不支持 json_object，改由提示词约束 JSON 输出")
        resp = await client.post(url, headers=headers, json=payload, timeout=timeout)

    if resp.status_code >= 400:
        detail = format_api_error(
            httpx.HTTPStatusError(
                "LLM error", request=resp.request, response=resp
            )
        )
        raise RuntimeError(f"LLM API {resp.status_code}：{detail}") from None

    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if not str(content).strip():
        raise RuntimeError("大模型返回空内容")
    return str(content).strip()


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("响应中未找到 JSON 对象")
    return json.loads(match.group())


def config_info() -> dict[str, Any]:
    return {
        "configured": is_configured(),
        "provider": llm_provider_label(),
        "base": api_base(),
        "model": default_model(),
        "lm_studio": is_lm_studio(),
    }
