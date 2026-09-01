"""Conversation command 与 Turn 的持久化编排。"""

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TurnRecord
from app.models.conversation_command_record import ConversationCommandRecord
from app.service import depends as service_depends
from app.service.task.conversation_command_service import (
    CommandDuplicateError,
    CommandPayloadConflictError,
)
from app.service.task.conversation_mutation_writer import ConversationMutationWriter
from app.storage.model.task_model import TaskModel
from app.storage.model.turn_model import TurnModel
from app.storage.store_engines import main_session_factory


class ActiveConversationRunError(ValueError):
    """同一 task 已有未结束运行时拒绝创建新运行。"""


@dataclass(frozen=True)
class ConversationRunStartResult:
    """一次 Assistant Transport 命令的持久化启动结果。

    命令重复（同 command_id 且同 payload）由 ``start`` 直接抛 ``CommandDuplicateError``，
    因此本结果只表示首次成功占用并创建绑定的场景。
    """

    command: ConversationCommandRecord
    turn: TurnRecord


class ConversationRunService:
    """在一个数据库事务中占用 command 并创建、绑定 Turn。"""

    def __init__(self) -> None:
        """初始化命令与轮次编排依赖。"""
        self._command = service_depends.get_conversation_command_crud()
        self._turn_service = service_depends.get_turn_service()
        self._session_factory = main_session_factory()
        self._mutation_writer = ConversationMutationWriter()

    def start(
        self,
        task_id: int,
        command_id: str,
        command_type: str,
        payload_hash: str,
        input_text: str,
        provider_id: int | None = None,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ConversationRunStartResult:
        """原子占用命令并创建绑定的 Turn。

        参数:
            task_id: 所属任务标识。
            command_id: Assistant Transport 命令幂等标识。
            command_type: Transport 命令类型。
            payload_hash: 命令业务载荷指纹。
            input_text: 用户输入文本。
            provider_id: 可选的模型厂商标识。
            model_name: 可选的模型名称。
            reasoning_effort: 可选的推理深度。

        返回:
            ``ConversationRunStartResult``，仅表示首次成功占用命令并创建绑定的
            Turn；重复命令（同 command_id 且同 payload）由本方法直接抛异常，不返回结果。

        异常:
            KeyError: 任务不存在。
            ValueError: Turn 业务校验失败。
            CommandDuplicateError: command_id 已被相同 payload 占用，禁止重复提交。
            CommandPayloadConflictError: command_id 已被不同 payload 占用。
            sqlalchemy.exc.SQLAlchemyError: 持久化失败。

        副作用:
            新命令在同一事务内写入 ``conversation_commands``、``turns`` 并完成绑定；
            任一步失败都会整体回滚，避免留下孤立命令或孤立 Turn。
        """
        existing = self._command.get(task_id, command_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise CommandPayloadConflictError(
                    f"command {command_id!r} already exists with a different payload"
                )
            raise CommandDuplicateError(
                f"command {command_id!r} was already submitted with the same payload"
            )

        try:
            with self._session_factory.begin() as session:
                self._ensure_task_exists(session, task_id)
                active_turn = (
                    session.query(TurnModel)
                    .filter(
                        TurnModel.task_id == task_id,
                        TurnModel.status.in_(("pending", "running")),
                    )
                    .first()
                )
                if active_turn is not None:
                    raise ActiveConversationRunError(
                        "当前任务已有运行中的对话，请等待完成或先取消"
                    )
                command = self._command.reserve_in_session(
                    session, task_id, command_id, command_type, payload_hash
                )
                turn = self._turn_service.create_turn(
                    task_id,
                    input_text,
                    agent_id="main_agent",
                    provider_id=provider_id,
                    model_name=model_name,
                    reasoning_effort=reasoning_effort,
                    session=session,
                )
                self._command.bind_turn_in_session(session, command.id, turn.id)
                self._mutation_writer.create_run_messages_in_session(
                    session, task_id, turn.id, input_text
                )
                return ConversationRunStartResult(command, turn)
        except IntegrityError:
            # 唯一索引是并发裁判；回滚后重新读取胜出的命令映射。
            existing = self._command.get(task_id, command_id)
            if existing is None:
                raise
            if existing.payload_hash != payload_hash:
                raise CommandPayloadConflictError(
                    f"command {command_id!r} already exists with a different payload"
                ) from None
            # 并发竞争下胜出的也是同 payload 重复命令，前端应已拦截，绕过则拒绝。
            raise CommandDuplicateError(
                f"command {command_id!r} was already submitted with the same payload"
            ) from None

    def get_existing_turn(
        self,
        task_id: int,
        command_id: str,
        payload_hash: str,
    ) -> TurnRecord:
        """返回同一幂等命令已经创建的 Turn，供断线重试重新订阅。

        参数:
            task_id: 命令所属任务标识。
            command_id: Assistant Transport 命令幂等标识。
            payload_hash: 当前请求的业务载荷指纹。

        返回:
            与命令绑定的 ``TurnRecord``。

        异常:
            KeyError: 命令不存在或命令尚未绑定 Turn。
            CommandPayloadConflictError: 命令标识对应的载荷不同。

        副作用:
            无（仅读取命令与 Turn 事实）。
        """
        command = self._command.get(task_id, command_id)
        if command is None or command.turn_id is None:
            raise KeyError(command_id)
        if command.payload_hash != payload_hash:
            raise CommandPayloadConflictError(
                f"command {command_id!r} already exists with a different payload"
            )
        return self._turn_service.get_turn(command.turn_id)

    @staticmethod
    def _ensure_task_exists(session: Session, task_id: int) -> None:
        """在写入命令前确认任务存在，区分 404 与并发唯一键冲突。"""
        if session.get(TaskModel, task_id) is None:
            raise KeyError(task_id)
