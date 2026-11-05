"""纯后台作业的登记与回投接线，终端后台命令和子 Agent 共用。

这里只登记已创建的作业并订阅结论，不启动执行者；前台等待与转后台归终端工具。
"""

from __future__ import annotations

from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.background.background_job import BackgroundConclusion, BackgroundJob
from app.models.message_metadata import BackgroundKind, BackgroundWork


def start_background_job(
    job: BackgroundJob, *, task_id: int, run_id: int | None, tool_call_id: str,
    kind: BackgroundKind, label: str,
) -> BackgroundWork:
    """登记纯后台作业，立即返回占位所需记录；结论由登记表交付。"""
    work = background_registry.register(
        tool_call_id, task_id=task_id, run_id=run_id, kind=kind, label=label,
        on_cancel=lambda _reason: job.cancel(),
    )
    def deliver(result: BackgroundConclusion) -> None:
        background_registry.complete(tool_call_id, result)

    job.on_done(deliver)
    return work
