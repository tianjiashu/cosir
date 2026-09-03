"""Assistant Transport 命令持久化值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_command_model import ConversationCommandModel
from app.utils.datetime_utils import from_text


@dataclass
class ConversationCommandRecord:
    """表示一次已接收的 Assistant Transport 命令。

    只承载命令自身的幂等占用事实与其驱动的 Run 标识；运行输入、模型路由、执行租约与
    终态结果属于 ``ConversationRunRecord``，不在本值对象内重复表达。
    """

    id: int
    task_id: int
    command_id: str
    command_type: str
    payload_hash: str
    run_id: int | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationCommandModel) -> "ConversationCommandRecord":
        """将 ORM 行映射为命令记录。

        参数:
            row: ``conversation_commands`` 表的行对象。

        返回:
            与该行等价的 ``ConversationCommandRecord``。

        异常:
            ValueError: 如果时间字段文本不符合约定格式（由 ``from_text`` 抛出）。

        副作用:
            无（纯映射，不触发额外数据库访问）。
        """
        return cls(
            id=row.id,
            task_id=row.task_id,
            command_id=row.command_id,
            command_type=row.command_type,
            payload_hash=row.payload_hash,
            run_id=row.run_id,
            error_code=row.error_code,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
