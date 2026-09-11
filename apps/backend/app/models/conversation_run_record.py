"""Conversation Run 持久化状态值对象。

单一职责：承载一次用户与 Agent 轮次的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/conversation_run_crud`` 负责）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

from app.models.json_helpers import (
    ConversationRunError,
    deserialize_json_object,
    deserialize_run_error,
    serialize_json_object,
    serialize_run_error,
)
from app.utils.datetime_utils import from_text, to_text

if TYPE_CHECKING:
    from app.storage.model.conversation_run_model import ConversationRunModel


class ConversationRunUsage(TypedDict):
    """Persisted six-field token usage contract for one run."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int | None
    reasoning_tokens: int


_USAGE_KEYS = frozenset(ConversationRunUsage.__annotations__)


def _validate_usage(value: object) -> ConversationRunUsage:
    """Validate the exact persisted run usage shape."""

    if not isinstance(value, dict) or frozenset(value) != _USAGE_KEYS:
        raise ValueError(
            "usage must contain exactly input_tokens, output_tokens, total_tokens, "
            "cache_hit_tokens, cache_miss_tokens, and reasoning_tokens"
        )
    for key, token_count in value.items():
        if token_count is None and key != "cache_miss_tokens":
            raise TypeError(f"usage.{key} must be a non-negative integer")
        if token_count is not None and (
            isinstance(token_count, bool) or not isinstance(token_count, int) or token_count < 0
        ):
            raise TypeError(
                f"usage.{key} must be a non-negative integer"
                + (" or null" if key == "cache_miss_tokens" else "")
            )
    return value  # type: ignore[return-value]


def serialize_run_usage(value: ConversationRunUsage | None) -> str | None:
    """Serialize the exact six-field run usage contract."""

    if value is None:
        return None
    return serialize_json_object(cast(dict[str, Any], _validate_usage(value)), "usage")


def deserialize_run_usage(raw: str | None) -> ConversationRunUsage | None:
    """Deserialize and validate a persisted run usage contract."""

    if raw is None:
        return None
    return _validate_usage(deserialize_json_object(raw, "usage"))


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
    extra: dict[str, Any] | None = None
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
            "extra": self.extra,
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
            extra=row.extra,
            usage=deserialize_run_usage(row.usage_json),
            error=(
                deserialize_run_error(row.error_json)
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
            "extra": self.extra,
            "usage_json": serialize_run_usage(self.usage),
            "error_json": (
                serialize_run_error(self.error) if self.error is not None else None
            ),
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
        if self.id:
            model_kwargs["id"] = self.id
        return ConversationRunModel(**model_kwargs)
