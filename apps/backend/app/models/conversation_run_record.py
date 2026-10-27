"""Conversation Run 持久化状态值对象。

单一职责：承载允许多条用户输入的一次 Agent 执行及持久进度并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/conversation_run_crud`` 负责）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from typing_extensions import TypedDict

from app.models.conversation_run_options import ConversationRunOptions
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.conversation_run_usage import ConversationRunUsage
from app.utils.datetime_utils import from_text, to_text

if TYPE_CHECKING:
    from app.storage.model.conversation_run_model import ConversationRunModel


class ConversationRunError(TypedDict):
    """Run 失败后持久化并投影给 UI 的错误契约。

    ``code`` 是内部稳定失败类别；``message`` 默认来自受控文案目录，模型 HTTP 错误时可以
    直接承载响应体中的 ``message`` 字段供 UI 展示。完整响应体和请求内容不进入此契约。
    """

    code: str
    message: str


@dataclass
class ConversationRunRecord:
    """表示一次 Agent 轮次；Agent 角色由 profile 注册表按 agent_id 解析，不冻结进 Run。"""

    id: int
    task_id: int
    status: str
    created_at: datetime
    updated_at: datetime
    execution_data: ReactExecutionState = field(default_factory=ReactExecutionState)
    end_reason: str | None = None
    final_output: str | None = None
    agent_id: str | None = None
    model_config_id: int | None = None
    context_window_total: int | None = None
    options: ConversationRunOptions = field(default_factory=ConversationRunOptions)
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
            "execution_data": self.execution_data.model_dump(mode="json"),
            "status": self.status,
            "end_reason": self.end_reason,
            "final_output": self.final_output,
            "agent_id": self.agent_id,
            "model_config_id": self.model_config_id,
            "context_window_total": self.context_window_total,
            "options": self.options.model_dump(mode="json"),
            "usage": self.usage,
            "error": self.error,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: ConversationRunModel) -> ConversationRunRecord:
        """在持久化读取边界校验选项与执行进度，非法结构抛 ValidationError。

        时间戳还原为 datetime；usage/error 解析为 JSON，非法文本抛 JSONDecodeError。
        不读取旧输入字段，也不从消息推导 Run 配置。
        """
        return cls(
            id=row.id,
            task_id=row.task_id,
            execution_data=ReactExecutionState.model_validate(row.execution_data),
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
            end_reason=row.end_reason,
            final_output=row.final_output,
            agent_id=row.agent_id,
            model_config_id=row.model_config_id,
            context_window_total=row.context_window_total,
            options=ConversationRunOptions.model_validate(row.options),
            usage=(json.loads(row.usage_json) if row.usage_json is not None else None),
            error=(json.loads(row.error_json) if row.error_json is not None else None),
        )

    def to_model(self) -> ConversationRunModel:
        """将 run 记录转换为 ORM 行，并严格序列化 JSON 字段。"""

        from app.storage.model.conversation_run_model import ConversationRunModel

        model_kwargs: dict[str, object] = {
            "task_id": self.task_id,
            "execution_data": self.execution_data.model_dump(mode="json"),
            "status": self.status,
            "end_reason": self.end_reason,
            "final_output": self.final_output,
            "agent_id": self.agent_id,
            "model_config_id": self.model_config_id,
            "context_window_total": self.context_window_total,
            "options": self.options.model_dump(mode="json"),
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
