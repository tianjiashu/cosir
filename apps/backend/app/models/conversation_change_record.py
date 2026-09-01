"""会话事实变更索引值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_change_model import ConversationChangeModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ConversationChangeRecord:
    """表示一次已经提交的会话事实变更索引。"""

    id: int
    task_id: int
    revision: int
    change_type: str
    entity_type: str
    entity_id: int | None
    turn_id: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationChangeModel) -> "ConversationChangeRecord":
        """从 ORM 行构造变更索引值对象。

        参数:
            row: ``conversation_changes`` 表 ORM 行。

        返回:
            对应的不可变变更记录。

        异常:
            ValueError: 如果数据库时间戳不是合法 ISO-8601 文本。

        副作用:
            无。
        """

        return cls(
            id=row.id,
            task_id=row.task_id,
            revision=row.revision,
            change_type=row.change_type,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            turn_id=row.turn_id,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
