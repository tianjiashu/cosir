"""Assistant Transport 命令幂等业务服务。"""

from sqlalchemy.exc import IntegrityError

from app.models.conversation_command_record import ConversationCommandRecord
from app.service import depends as service_depends


class CommandPayloadConflictError(ValueError):
    """同一 task 下 command id 对应了不同业务载荷。"""


class CommandDuplicateError(ValueError):
    """同一 task 下 command id 已被相同业务载荷占用，禁止重复提交。"""


class ConversationCommandService:
    """管理 Transport command 的占用、校验和 turn 绑定。"""

    def __init__(self) -> None:
        """初始化命令服务并绑定命令 CRUD。"""
        self._command = service_depends.get_conversation_command_crud()

    def reserve_or_get(
        self, task_id: int, command_id: str, command_type: str, payload_hash: str
    ) -> tuple[ConversationCommandRecord, bool]:
        """占用命令 ID，重复命令返回既有记录。

        返回元组第二项表示是否首次创建；并发竞争由数据库唯一索引裁决。
        """
        existing = self._command.get(task_id, command_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise CommandPayloadConflictError(
                    f"command {command_id!r} already exists with a different payload"
                ) from None
            return existing, False
        try:
            return (
                self._command.reserve(task_id, command_id, command_type, payload_hash),
                True,
            )
        except IntegrityError:
            existing = self._command.get(task_id, command_id)
            if existing is None:
                raise
            if existing.payload_hash != payload_hash:
                raise CommandPayloadConflictError(
                    f"command {command_id!r} already exists with a different payload"
                ) from None
            return existing, False

    def bind_turn(self, command: ConversationCommandRecord, turn_id: int) -> None:
        """把命令绑定到后端 turn。"""
        self._command.bind_turn(command.id, turn_id)

    def mark_failed(self, command: ConversationCommandRecord, error_code: str) -> None:
        """将命令标记为失败并保留幂等记录。"""
        self._command.mark_failed(command.id, error_code)

    def mark_status(self, command: ConversationCommandRecord, status: str) -> None:
        """同步命令的运行终态。

        参数:
            command: 要更新的持久化命令记录。
            status: ``completed``、``failed`` 或 ``cancelled``。

        返回:
            无。

        异常:
            ValueError: 状态不是允许的命令终态。
            sqlalchemy.exc.SQLAlchemyError: 底层更新失败。

        副作用:
            更新 ``conversation_commands`` 中对应行的状态和更新时间。
        """
        self._command.mark_status(command.id, status)

    def mark_status_for_turn(self, turn_id: int, status: str) -> None:
        """以运行标识终结其绑定命令；不存在绑定命令时不产生写入。"""
        command = self._command.get_by_turn(turn_id)
        if command is not None and command.status == "processing":
            self._command.mark_status(command.id, status)
