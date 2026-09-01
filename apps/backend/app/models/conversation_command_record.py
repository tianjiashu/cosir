"""Assistant Transport 命令持久化值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_command_model import ConversationCommandModel
from app.utils.datetime_utils import from_text


@dataclass
class ConversationCommandRecord:
    """表示一次已接收的 Assistant Transport 命令。"""

    id: int
    task_id: int
    command_id: str
    command_type: str
    payload_hash: str
    turn_id: int | None
    status: str
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationCommandModel) -> "ConversationCommandRecord":
        """将 ORM 行映射为命令记录。"""
        return cls(
            id=row.id,
            task_id=row.task_id,
            command_id=row.command_id,
            command_type=row.command_type,
            payload_hash=row.payload_hash,
            turn_id=row.turn_id,
            status=row.status,
            error_code=row.error_code,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
