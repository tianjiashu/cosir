"""Conversation Run 持久化状态值对象。

单一职责：承载一次用户与 Agent 轮次的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/conversation_run_crud`` 负责）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from typing_extensions import TypedDict

from app.models.conversation_run_extra import ConversationRunExtra
from app.models.conversation_run_usage import ConversationRunUsage
from app.utils.datetime_utils import from_text, to_text

if TYPE_CHECKING:
    from app.storage.model.conversation_run_model import ConversationRunModel



class ConversationRunError(TypedDict):
    """Controlled, user-safe error contract persisted for a failed run."""

    code: str
    message: str
    retryable: bool


@dataclass
class ConversationRunRecord:
    """表示一次用户与 Agent 的轮次。"""

    id: int
    task_id: int
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime
    # 与数据库 run 主键分离的 LangGraph thread 身份；checkpoint 生命周期可能长于主库自增 ID。
    checkpoint_thread_id: str
    end_reason: str | None = None
    final_output: str | None = None
    agent_id: str | None = None
    provider_id: int | None = None
    model_name: str | None = None
    image_paths: list[str] | None = None
    reasoning_effort: str | None = None
    extra: ConversationRunExtra | None = None
    usage: ConversationRunUsage | None = None
    error: ConversationRunError | None = None

    def to_dict(self) -> dict[str, object]:
        """将轮次状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            包含轮次字段的字典；``usage`` 与 ``error`` 保留为 typed JSON 对象或 None。

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
            "final_output": self.final_output,
            "image_paths": self.image_paths,
            "reasoning_effort": self.reasoning_effort,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "extra": self.extra.to_dict() if self.extra is not None else None,
            "usage": self.usage,
            "error": self.error,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
            "checkpoint_thread_id": self.checkpoint_thread_id,
        }

    @classmethod
    def from_model(cls, row: ConversationRunModel) -> ConversationRunRecord:
        """从 ORM 行构造轮次记录值对象。

        参数:
            row: ``conversation_runs`` 表的行对象。

        返回:
            对应的 ``ConversationRunRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

        异常:
            json.JSONDecodeError: 如果持久化的 usage/error JSON 非法。
            TypeError: 如果 usage/error 结构不符合 typed JSON 契约。

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
            checkpoint_thread_id=row.checkpoint_thread_id,
            end_reason=row.end_reason,
            final_output=row.final_output,
            agent_id=row.agent_id,
            image_paths=row.image_paths,
            reasoning_effort=row.reasoning_effort,
            model_name=row.model_name,
            provider_id=row.provider_id,
            extra=ConversationRunExtra.from_dict(row.extra),
            usage=(
                json.loads(row.usage_json)
                if row.usage_json is not None
                else None
            ),
            error=(
                json.loads(row.error_json)
                if row.error_json is not None
                else None
            ),
        )

    def to_model(self) -> ConversationRunModel:
        """将 run 记录转换为 ORM 行，并严格序列化 JSON 字段。"""

        from app.storage.model.conversation_run_model import ConversationRunModel

        model_kwargs: dict[str, object] = {
            "task_id": self.task_id,
            "input_text": self.input_text,
            "status": self.status,
            "checkpoint_thread_id": self.checkpoint_thread_id,
            "end_reason": self.end_reason,
            "final_output": self.final_output,
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "image_paths": self.image_paths,
            "reasoning_effort": self.reasoning_effort,
            "extra": self.extra.to_dict() if self.extra is not None else None,
            "usage_json": json.dumps(
                self.usage, ensure_ascii=False, sort_keys=True, allow_nan=False
            ),
            "error_json": (
                json.dumps(self.error, ensure_ascii=False, sort_keys=True, allow_nan=False)
                if self.error is not None
                else None
            ),
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
        if self.id:
            model_kwargs["id"] = self.id
        return ConversationRunModel(**model_kwargs)
