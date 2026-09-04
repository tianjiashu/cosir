"""Conversation Run 持久化状态值对象。

单一职责：承载一次用户与 Agent 轮次的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/conversation_run_crud`` 负责）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.utils.datetime_utils import from_text, to_text

if TYPE_CHECKING:
    from app.storage.model.conversation_run_model import ConversationRunModel


@dataclass
class ConversationRunRecord:
    """表示一次用户与 Agent 的轮次。"""

    id: int
    task_id: int
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime
    end_reason: str | None = None
    agent_id: str | None = None
    provider_id: int | None = None
    model_name: str | None = None
    image_paths: list[str] | None = None
    reasoning_effort: str | None = None
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, object]:
        """将轮次状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含轮次字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "id": self.id,
            "task_id": self.task_id,
            "input_text": self.input_text,
            "status": self.status,
            "end_reason": self.end_reason,
            "image_paths": self.image_paths,
            "reasoning_effort": self.reasoning_effort,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "extra": self.extra,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: ConversationRunModel) -> ConversationRunRecord:
        """从 ORM 行构造轮次记录值对象。

        参数:
            row: ``conversation_runs`` 表的行对象。

        返回:
            对应的 ``ConversationRunRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            无。

        副作用:
            无。
        """
        return cls(
            id=row.id,
            task_id=row.task_id,
            input_text=row.input_text,
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            end_reason=row.end_reason,
            agent_id=row.agent_id,
            image_paths=row.image_paths,
            reasoning_effort=row.reasoning_effort,
            model_name=row.model_name,
            provider_id=row.provider_id,
            extra=row.extra,
        )
