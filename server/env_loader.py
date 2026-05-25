"""从项目根目录加载 .env（不依赖 python-dotenv）。"""

from __future__ import annotations

import os
from pathlib import Path


def load_project_env() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    env_path = root / ".env"
    if not env_path.is_file():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        value = value.strip("'\"").strip("'\u2018\u2019\"\u201c\u201d")
        if key and key not in os.environ:
            os.environ[key] = value
    return env_path
