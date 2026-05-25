"""站外素材：快手优先，失败则抖音。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from douyin_material import fetch_douyin_body_source
from kuaishou_material import fetch_kuaishou_body_source
from material_common import is_douyin_url, is_kuaishou_url

logger = logging.getLogger(__name__)


async def fetch_external_body_source(
    client: httpx.AsyncClient,
    *,
    drama_title: str,
    keyword: str = "",
    share_url: str = "",
    dest_dir: Path,
) -> dict[str, Any]:
    """
    获取推广正片素材：优先快手；指定链接则按平台解析；
    快手搜索失败时自动改抖音搜索。
    """
    share = share_url.strip()
    errors: list[str] = []

    if share and is_douyin_url(share):
        logger.info("使用抖音分享链接下载素材")
        return await fetch_douyin_body_source(
            client,
            drama_title=drama_title,
            keyword=keyword,
            share_url=share,
            dest_dir=dest_dir / "douyin",
        )

    if share and is_kuaishou_url(share):
        logger.info("使用快手分享链接下载素材")
        return await fetch_kuaishou_body_source(
            client,
            drama_title=drama_title,
            keyword=keyword,
            share_url=share,
            dest_dir=dest_dir / "kuaishou",
        )

    try:
        logger.info("尝试从快手搜索素材: %s", drama_title)
        return await fetch_kuaishou_body_source(
            client,
            drama_title=drama_title,
            keyword=keyword,
            share_url="",
            dest_dir=dest_dir / "kuaishou",
        )
    except Exception as exc:
        errors.append(f"快手: {exc}")
        logger.warning("快手素材失败，改搜抖音: %s", exc)

    try:
        logger.info("尝试从抖音搜索素材: %s", drama_title)
        return await fetch_douyin_body_source(
            client,
            drama_title=drama_title,
            keyword=keyword,
            share_url="",
            dest_dir=dest_dir / "douyin",
        )
    except Exception as exc:
        errors.append(f"抖音: {exc}")

    raise RuntimeError(
        "快手与抖音均未获取到可用素材。"
        + " ".join(errors)
        + " 请配置 HONGGUO_KUAISHOU_COOKIE 或 HONGGUO_DOUYIN_COOKIE，"
        "或在 App 内复制作品分享链接。"
    )
