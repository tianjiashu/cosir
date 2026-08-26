"""Persisted delegation state value object."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.storage.model.delegation_model import DelegationModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class DelegationRecord:
    """Represent one parent-to-child Agent delegation request.

    child_task_id: 委派子任务 task 标识（整数外键）。pending 阶段委派尚未真正创建子 task，
        该字段为 0；running/终态（completed/failed/cancelled）阶段由
        DelegationService 写入真实子 task 标识。crud 层读取时以
        ``row.child_task_id or 0`` 兜底，与默认值保持一致，避免 None 穿透。
    """

    id: int | None
    task_id: int
    parent_turn_id: int | None
    child_turn_id: int | None
    parent_agent_id: str | None
    child_agent_id: str
    status: str
    prompt: str
    summary: str
    error: str
    effective_tools: tuple[str, ...]
    # 带默认值的字段必须排在 dataclass 末尾；pending 阶段委派尚未真正创建子 task，
    # 该字段为 0，running/终态由 DelegationService 写入真实子 task 标识。
    child_task_id: int | None
    # 时间字段由存储层 server_default 填充，构造时通常留空；``from_model`` 会从
    # ORM 行回填。带默认值排在末尾，避免破坏既有无默认值字段的构造点。
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_model_dict(self) -> dict[str, Any]:
        """转换为 ``delegations`` 表列值字典。

        供存储层 ``DelegationModel(**record.to_model_dict())`` 或
        ``insert(DelegationModel).values(**record.to_model_dict())`` 构造/插入使用。
        ``id`` 为 ``None`` 时仍包含在字典中（SQLAlchemy 会忽略自增列），
        ``created_at``/``updated_at`` 故意省略，交由数据库 server_default 填充。

        参数:
            无。

        返回:
            键为 ``DelegationModel`` 列名、值为已序列化列的字典。

        异常:
            TypeError: 如果 ``effective_tools`` 包含不可 JSON 序列化的值。

        副作用:
            无。
        """

        return {
            "id": self.id,
            "task_id": self.task_id,
            "parent_turn_id": self.parent_turn_id,
            "child_turn_id": self.child_turn_id,
            "child_task_id": self.child_task_id,
            "parent_agent_id": self.parent_agent_id,
            "child_agent_id": self.child_agent_id,
            "status": self.status,
            "prompt": self.prompt,
            "summary": self.summary,
            "error": self.error,
            "effective_tools": json.dumps(self.effective_tools, ensure_ascii=False),
        }

    @classmethod
    def from_model(cls, row: DelegationModel) -> "DelegationRecord":
        """从 ORM 行构造委派记录值对象。

        参数:
            row: ``delegations`` 表的 SQLAlchemy 行对象。

        返回:
            对应的不可变 ``DelegationRecord``；``child_task_id`` 以
            ``row.child_task_id or 0`` 兜底，避免 None 穿透；``effective_tools``
            由 JSON 文本反序列化为元组；时间字段经 ``from_text`` 解析。

        异常:
            json.JSONDecodeError: 如果存储的工具 JSON 无法解析。

        副作用:
            无。
        """
        return cls(
            id=row.id,
            task_id=row.task_id,
            parent_turn_id=row.parent_turn_id,
            child_turn_id=row.child_turn_id,
            parent_agent_id=row.parent_agent_id,
            child_agent_id=row.child_agent_id,
            status=row.status,
            prompt=row.prompt,
            summary=row.summary,
            error=row.error,
            effective_tools=tuple(json.loads(row.effective_tools or "[]")),
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            child_task_id=row.child_task_id or 0,
        )
