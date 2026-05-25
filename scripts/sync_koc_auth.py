#!/usr/bin/env python3
"""命令行同步番茄达人中心权限（需已 pip install playwright && playwright install chromium）。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from env_loader import load_project_env
from fq_koc_browser import sync_koc_auth_via_browser
from fq_koc_session import load_koc_session


async def main() -> None:
    load_project_env()
    load_koc_session()
    p = argparse.ArgumentParser(description="浏览器同步达人中心下载权限")
    p.add_argument("series_id", help="短剧 book_id / series_id")
    p.add_argument("item_id", help="分集 item_id")
    p.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（需曾用可见浏览器登录过）",
    )
    args = p.parse_args()
    result = await sync_koc_auth_via_browser(
        args.series_id,
        args.item_id,
        open_browser=not args.headless,
    )
    print("OK", result)


if __name__ == "__main__":
    asyncio.run(main())
