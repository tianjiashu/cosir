"""会话消息 part 事实值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_message_part_model import ConversationMessagePartModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ConversationMessagePartRecord:
    """表示一条消息中的有序内容 part。"""

    id: int
    message_id: int
    sequence: int
    part_type: str
    text: str | None
    data_json: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationMessagePartModel) -> "ConversationMessagePartRecord":
        """从 ORM 行构造消息 part 值对象。

        参数:
            row: ``conversation_message_parts`` 表 ORM 行。

        返回:
            对应的不可变消息 part 记录。

        异常:
            ValueError: 如果数据库时间戳不是合法 ISO-8601 文本。

        副作用:
            无。
        """

        return cls(
            id=row.id,
            message_id=row.message_id,
            sequence=row.sequence,
            part_type=row.part_type,
            text=row.text,
            data_json=row.data_json,
            status=row.status,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
