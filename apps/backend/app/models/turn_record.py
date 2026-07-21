"""轮次持久化状态值对象。

单一职责：承载一次用户与 Agent 轮次的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/turn_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime

from app.utils.datetime_utils import to_text


@dataclass
class TurnRecord:
    """表示一次用户与 Agent 的轮次。"""

    turn_id: str
    task_id: str
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, str]:
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
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "input_text": self.input_text,
            "status": self.status,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }
