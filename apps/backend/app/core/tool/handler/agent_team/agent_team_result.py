"""Team 落定后交给主 Agent 的工具结果：状态、结束原因与运行档案的读取方法。

只给档案路径和分段读取说明，不把节点输出内联进结果：整份历史可能远超单次工具结果的预算，
由 Agent 按需用 ``read_file`` 分页读取。
"""

from __future__ import annotations

from typing import Any

from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.core.tool.handler.agent_team.agent_team_run_archive_service import AgentTeamRunArchiveService
from app.storage.model.agent_team_run_model import AgentTeamRunModel


def team_result_payload(row: AgentTeamRunModel) -> dict[str, Any]:
    """落定的 TeamRun → 工具结果正文；档案未生成时只说明不可用，不抛异常。"""
    archive_path = AgentTeamRunArchiveService().existing_archive_path(row.id)
    payload: dict[str, Any] = {
        "team_run_id": row.id,
        "status": row.status,
        "end_reason": row.end_reason,
        "trace_file": archive_path,
        "read_instructions": _archive_read_instructions(archive_path),
    }
    if row.status == AgentTeamRunStatus.COMPLETED.value:
        payload["hint"] = "Review the node outputs in the saved run history against the goal."
    return payload


def _archive_read_instructions(archive_path: str | None) -> str:
    """给 Agent 返回现有 read_file 的分段读取方法，不要求整份档案一次装入上下文。"""

    if archive_path is None:
        return "The run history file is not available."
    return (
        f'Read with read_file(path="{archive_path}", offset=1, limit=200). '
        "Inspect the top-level status and failure first, then read history in pages; "
        "when the result says to continue with a later offset, use that offset rather "
        "than loading the whole file. Each history entry contains one node's status, "
        "output, decision, and next node. If the file exceeds read_file's size limit or "
        "an output line is truncated, use execute_terminal with Python's json module to "
        "load the document and print only the needed history entry or a small output slice."
    )
