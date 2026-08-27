"""轮次持久化状态值对象。

单一职责：承载一次用户与 Agent 轮次的不可变业务数据并提供序列化（to_dict）。
不负责数据库操作（由 ``storage/crud/turn_crud`` 负责）。
"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.turn_model import TurnModel
from app.utils.datetime_utils import from_text, to_text


@dataclass
class TurnRecord:
    """表示一次用户与 Agent 的轮次。"""

    id: int
    task_id: int
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime
    end_reason: str | None = None
    response_text: str | None = None
    agent_id: str | None = None
    product_id: int | None = None
    model_name: str | None = None
    paths: list[str] | None = None
    reasoning_effort: str | None = None

    def to_dict(self) -> dict[str, str | None]:
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
            "response_text": self.response_text,
            "paths": self.paths,
            "reasoning_effort": self.reasoning_effort,
            "agent_id": self.agent_id,
            "product_name": self.product_id,
            "model_name": self.model_name,
            "created_at": to_text(self.created_at),
            "updated_at": to_text(self.updated_at),
        }

    @classmethod
    def from_model(cls, row: TurnModel) -> "TurnRecord":
        """从 ORM 行构造轮次记录值对象。

        参数:
            row: ``turns`` 表的 SQLAlchemy 行对象。

        返回:
            对应的 ``TurnRecord``；文本时间戳经 ``from_text`` 还原为 datetime。

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
            response_text=row.response_text,
            agent_id=row.agent_id,
            paths=row.paths,
            reasoning_effort=row.reasoning_effort,
            model_name=row.model_name,
            product_id=row.product_id,
        )
