"""Persisted delegation state value object."""

import json
from dataclasses import dataclass
from datetime import datetime

from app.storage.model.delegation_model import DelegationModel
from app.utils.datetime_utils import from_text


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

    @classmethod
    def from_model(cls, row: DelegationModel) -> "DelegationRecord":
        """从 ORM 行构造委派记录值对象。

        参数:
            row: ``delegations`` 表的 SQLAlchemy 行对象。

        返回:
            对应的不可变 ``DelegationRecord``；``child_task_id`` 以
            ``row.child_task_id or ""`` 兜底，避免 None 穿透；``effective_tools``
            由 JSON 文本反序列化为元组；时间字段经 ``from_text`` 解析。

        异常:
            json.JSONDecodeError: 如果存储的工具 JSON 无法解析。

        副作用:
            无。
        """
        return cls(
            delegation_id=row.delegation_id,
            task_id=row.task_id,
            parent_turn_id=row.parent_turn_id,
            child_turn_id=row.child_turn_id,
            parent_agent_id=row.parent_agent_id,
            child_agent_id=row.child_agent_id,
            delegation_type=row.delegation_type,
            status=row.status,
            prompt=row.prompt,
            summary=row.summary,
            error=row.error,
            effective_tools=tuple(json.loads(row.effective_tools or "[]")),
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            child_task_id=row.child_task_id or "",
        )
