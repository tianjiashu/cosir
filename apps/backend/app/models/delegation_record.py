"""Persisted delegation state value object."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DelegationRecord:
    """Represent one parent-to-child Agent delegation request.

    child_task_id: 委派子任务 task 标识。pending 阶段委派尚未真正创建子 task，
        该字段为空串 ""；running/终态（completed/failed/cancelled）阶段由
        DelegationService 写入真实子 task 标识。crud 层读取时以
        ``row.child_task_id or ""`` 兜底，与默认值保持一致，避免 None 穿透。
    """

    delegation_id: str
    task_id: str
    parent_turn_id: str
    child_turn_id: str
    parent_agent_id: str
    child_agent_id: str
    delegation_type: str
    status: str
    prompt: str
    summary: str
    error: str
    effective_tools: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    # 带默认值的字段必须排在 dataclass 末尾；pending 阶段委派尚未真正创建子 task，
    # 该字段为空串 ""，running/终态由 DelegationService 写入真实子 task 标识。
    child_task_id: str = ""
