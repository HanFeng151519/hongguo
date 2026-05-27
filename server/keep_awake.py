"""成片生成期间防熄屏：定时微移鼠标（默认每 3 分钟 1 像素）。"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_guard: Optional["KeepAwakeGuard"] = None


def keep_awake_enabled() -> bool:
    v = os.getenv("HONGGUO_KEEP_AWAKE", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def keep_awake_interval_sec() -> float:
    raw = os.getenv("HONGGUO_KEEP_AWAKE_INTERVAL_SEC", "180").strip()
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 180.0


def keep_awake_nudge_px() -> int:
    raw = os.getenv("HONGGUO_KEEP_AWAKE_NUDGE_PX", "1").strip()
    try:
        return max(1, min(20, int(raw)))
    except ValueError:
        return 1


def _nudge_mouse_darwin(pixels: int) -> bool:
    delta = max(1, int(pixels))
    script = f"""
tell application "System Events"
    set p to position of the mouse
    set x to (item 1 of p) + {delta}
    set y to item 2 of p
    set position of the mouse to {{x, y}}
    delay 0.03
    set position of the mouse to p
end tell
""".strip()
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _nudge_mouse_linux(pixels: int) -> bool:
    delta = max(1, int(pixels))
    try:
        proc = subprocess.run(
            ["xdotool", "getmouselocation", "--shell"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return False
        x = y = 0
        for line in (proc.stdout or "").splitlines():
            if line.startswith("X="):
                x = int(line.split("=", 1)[1])
            elif line.startswith("Y="):
                y = int(line.split("=", 1)[1])
        subprocess.run(
            ["xdotool", "mousemove", str(x + delta), str(y)],
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.03)
        subprocess.run(
            ["xdotool", "mousemove", str(x), str(y)],
            capture_output=True,
            timeout=5,
        )
        return True
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


def nudge_mouse(pixels: Optional[int] = None) -> bool:
    px = keep_awake_nudge_px() if pixels is None else max(1, int(pixels))
    system = platform.system()
    if system == "Darwin":
        return _nudge_mouse_darwin(px)
    if system == "Linux":
        return _nudge_mouse_linux(px)
    logger.debug("防熄屏：当前系统 %s 未实现鼠标微移", system)
    return False


class KeepAwakeGuard:
    def __init__(
        self,
        *,
        interval_sec: Optional[float] = None,
        nudge_px: Optional[int] = None,
    ) -> None:
        self.interval_sec = (
            keep_awake_interval_sec() if interval_sec is None else float(interval_sec)
        )
        self.nudge_px = keep_awake_nudge_px() if nudge_px is None else int(nudge_px)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="hongguo_keep_awake",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "防熄屏已开启：每 %.0f 秒微移鼠标 %d 像素",
            self.interval_sec,
            self.nudge_px,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        # 启动后先轻推一次，避免刚开任务就休眠
        if nudge_mouse(self.nudge_px):
            logger.debug("防熄屏：已执行鼠标微移")
        while not self._stop.wait(self.interval_sec):
            if nudge_mouse(self.nudge_px):
                logger.debug("防熄屏：已执行鼠标微移")
            else:
                logger.warning(
                    "防熄屏：鼠标微移失败（macOS 需给终端/IDE「辅助功能」权限）"
                )


def start_keep_awake() -> Optional[KeepAwakeGuard]:
    if not keep_awake_enabled():
        return None
    global _guard
    guard = KeepAwakeGuard()
    guard.start()
    _guard = guard
    return guard


def stop_keep_awake(guard: Optional[KeepAwakeGuard]) -> None:
    global _guard
    if guard is not None:
        guard.stop()
    if _guard is guard:
        _guard = None


@contextmanager
def keep_awake_session() -> Iterator[Optional[KeepAwakeGuard]]:
    guard = start_keep_awake()
    try:
        yield guard
    finally:
        stop_keep_awake(guard)
