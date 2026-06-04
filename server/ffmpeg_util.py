"""跨平台解析 ffmpeg / ffprobe（Windows 无 PATH 时用 imageio-ffmpeg；macOS 兼容 Homebrew）。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_ffmpeg_cached: str | None = None
_ffprobe_cached: str | None = None

_MAC_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def _mac_tool(name: str) -> Optional[str]:
    for d in _MAC_BIN_DIRS:
        p = Path(d) / name
        if p.is_file():
            return str(p)
    return None


def _sibling_ffprobe(ffmpeg_exe: str) -> Optional[str]:
    parent = Path(ffmpeg_exe).parent
    for name in ("ffprobe.exe", "ffprobe"):
        p = parent / name
        if p.is_file():
            return str(p)
    return None


def resolve_ffmpeg_exe() -> str:
    """优先 FFMPEG 环境变量 → imageio-ffmpeg → PATH → macOS Homebrew。"""
    global _ffmpeg_cached
    if _ffmpeg_cached and Path(_ffmpeg_cached).is_file():
        return _ffmpeg_cached

    env = os.getenv("FFMPEG", "").strip()
    if env and Path(env).is_file():
        _ffmpeg_cached = env
        return env

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(exe).is_file():
            _ffmpeg_cached = exe
            return exe
    except Exception:
        pass

    found = shutil.which("ffmpeg")
    if found and Path(found).is_file():
        _ffmpeg_cached = found
        return found

    mac = _mac_tool("ffmpeg")
    if mac:
        _ffmpeg_cached = mac
        return mac

    hint = (
        "pip install imageio-ffmpeg"
        if is_windows()
        else "brew install ffmpeg 或 pip install imageio-ffmpeg"
    )
    raise RuntimeError(f"未找到 ffmpeg。请执行: {hint}，或在 .env 设置 FFMPEG=可执行文件完整路径")


def resolve_ffprobe_exe() -> Optional[str]:
    """ffprobe 可选；无则回退用 ffmpeg -i 解析时长。"""
    global _ffprobe_cached
    if _ffprobe_cached and Path(_ffprobe_cached).is_file():
        return _ffprobe_cached

    env = os.getenv("FFPROBE", "").strip()
    if env and Path(env).is_file():
        _ffprobe_cached = env
        return env

    found = shutil.which("ffprobe")
    if found and Path(found).is_file():
        _ffprobe_cached = found
        return found

    try:
        sib = _sibling_ffprobe(resolve_ffmpeg_exe())
        if sib:
            _ffprobe_cached = sib
            return sib
    except RuntimeError:
        pass

    mac = _mac_tool("ffprobe")
    if mac:
        _ffprobe_cached = mac
        return mac

    return None


def resolve_mp4decrypt_exe() -> Optional[str]:
    """Bento4 mp4decrypt（仅解密红果加密源时需要；推广中心明文可跳过）。"""
    env = os.getenv("MP4DECRYPT", "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("mp4decrypt")
    if found and Path(found).is_file():
        return found
    return _mac_tool("mp4decrypt")


def cjk_font_paths(*, bold: bool = False) -> list[str]:
    """片头/水印等中文字体：Windows 微软雅黑、macOS PingFang、Linux Noto。"""
    paths: list[str] = []
    if is_windows():
        if bold:
            paths.append(r"C:\Windows\Fonts\msyhbd.ttc")
        paths.extend(
            [
                r"C:\Windows\Fonts\msyh.ttc",
                r"C:\Windows\Fonts\simhei.ttf",
                r"C:\Windows\Fonts\msyhl.ttc",
            ]
        )
    if is_macos():
        if bold:
            paths.extend(
                [
                    "/System/Library/Fonts/STHeiti Medium.ttc",
                    "/System/Library/Fonts/PingFang.ttc",
                ]
            )
        paths.extend(
            [
                "/System/Library/Fonts/PingFang.ttc",
                "/System/Library/Fonts/Hiragino Sans GB.ttc",
            ]
        )
    paths.append("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc")
    paths.append("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    return paths


def configure_ffmpeg_env() -> str:
    """服务启动时调用：供 MoviePy / imageio 使用同一 ffmpeg。"""
    exe = resolve_ffmpeg_exe()
    os.environ["IMAGEIO_FFMPEG_EXE"] = exe
    os.environ["FFMPEG_BINARY"] = exe
    probe = resolve_ffprobe_exe()
    if probe:
        os.environ["FFPROBE_BINARY"] = probe
    return exe
