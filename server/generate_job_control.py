"""生成任务：中断后进入手动时间轴剪辑。"""

from __future__ import annotations

from typing import Any, Optional


class JobInterruptedForEdit(Exception):
    """正片已下载，用户请求中断以手动调整剪辑方案。"""

    def __init__(self, edit_plan: dict[str, Any]):
        self.edit_plan = edit_plan
        super().__init__("interrupted_for_manual_edit")
