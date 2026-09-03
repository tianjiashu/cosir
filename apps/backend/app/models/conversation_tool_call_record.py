"""会话工具调用事实值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_tool_call_model import ConversationToolCallModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ConversationToolCallRecord:
    """表示一次可恢复的结构化工具调用事实。"""

    id: int
    task_id: int
    run_id: int | None
    message_id: int | None
    part_id: int | None
    tool_call_id: str
    tool_name: str
    args_json: str
    result_json: str | None
    status: str
    error_text: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationToolCallModel) -> "ConversationToolCallRecord":
        """从 ORM 行构造工具调用事实值对象。

        参数:
            row: ``conversation_tool_calls`` 表 ORM 行。

        返回:
            对应的不可变工具调用记录。

        异常:
            ValueError: 如果数据库时间戳不是合法 ISO-8601 文本。

        副作用:
            无。
        """

        return cls(
            id=row.id,
            task_id=row.task_id,
            run_id=row.run_id,
            message_id=row.message_id,
            part_id=row.part_id,
            tool_call_id=row.tool_call_id,
            tool_name=row.tool_name,
            args_json=row.args_json,
            result_json=row.result_json,
            status=row.status,
            error_text=row.error_text,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
