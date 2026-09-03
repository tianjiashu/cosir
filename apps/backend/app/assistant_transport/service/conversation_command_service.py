"""Conversation command 与 Conversation Run 的持久化编排。"""

import copy
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.models import ConversationRunRecord, TaskRecord
from app.models.conversation_command_record import ConversationCommandRecord
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends
from app.assistant_transport.service.conversation_mutation_writer import ConversationMutationWriter
from app.assistant_transport.service.conversation_snapshot_service import ConversationTaskSnapshotService
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import preview


@dataclass(frozen=True)
class ConversationRunStartResult:
    """一次 Assistant Transport 命令的持久化启动结果。

    命令重复（同 command_id 且同 payload）由 ``start`` 直接抛 ``CommandDuplicateError``，
    因此本结果只表示首次成功占用并创建绑定的场景。
    """

    command: ConversationCommandRecord
    run: ConversationRunRecord
    task: TaskRecord | None = None


class ConversationCommandService:
    """在一个数据库事务中占用 command 并创建、绑定 ConversationRun。"""

    def __init__(self) -> None:
        """初始化命令与轮次编排依赖。"""
        self._command = service_depends.get_conversation_command_crud()
        self._run = service_depends.get_conversation_run_crud()
        self._session_factory = main_session_factory()
        self._mutation_writer = ConversationMutationWriter()
        self._snapshots = ConversationTaskSnapshotService()
        self._task = service_depends.get_task_crud()

    def start(
        self,
        command_id: str,
        command_type: str,
        payload_hash: str,
        input_text: str,
        provider_id: int,
        model_name: str,
        reasoning_effort: str | None = None,
        task_id: int | None = None,
        workspace_id: int | None = None,
        initial_state: ConversationStateSnapshot | None = None,
    ) -> ConversationRunStartResult:
        """原子占用命令并创建、绑定 Conversation Run，统一处理新建对话与续接已有任务。

        新建对话（``task_id is None``）会在同一事务内创建 Task、command、run 与
        canonical 消息事实；续接已有任务（``task_id is not None``）只创建 command 与
        run。两条路径的尾部（reserve command → create run → bind → 写入消息事实）共享
        同一实现。``provider_id`` / ``model_name`` 的必填校验已由 ``AssistantTransportRequest``
        在 wire 边界完成，本方法不再重复校验（见 ``validate_transport_constraints``）。

        参数:
            command_id: Assistant Transport 命令幂等标识。
            command_type: Transport 命令类型。
            payload_hash: 命令业务载荷指纹。
            input_text: 用户输入文本（非空由 request 层 ``AssistantTextPart`` 保证）。
            provider_id: 模型厂商标识。
            model_name: 模型名称。
            reasoning_effort: 可选的推理深度。
            task_id: 续接任务的标识；``None`` 表示新建对话。
            workspace_id: 新建对话所属工作区；仅 ``task_id is None`` 时生效。

        返回:
            新建返回 ``NewConversationStartResult``（含 Task），续接返回
            ``ConversationRunStartResult``；重复命令（同 command_id 且同 payload）直接抛
            异常，不返回结果。

        异常:
            KeyError: 任务或工作区不存在。
            ValueError: 工作区不存在导致的命令认领失败等非法状态。
            CommandDuplicateError: command_id 已被相同 payload 占用，禁止重复提交。
            CommandPayloadConflictError: command_id 已被不同 payload 占用。
            sqlalchemy.exc.SQLAlchemyError: 持久化失败。

        副作用:
            新命令在同一事务内写入并更新 ``conversation_commands``，完成 command/run 绑定；
            新建场景额外原子写入 task 与 canonical 消息事实；任一步失败整体回滚。
        """
        # 前置重复命令判定：两条路径一致（同 command_id 同 payload 视为重复提交）。
        if task_id is not None:
            existing = self._command.get(task_id, command_id)
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise RuntimeError(
                        f"command {command_id!r} already exists with a different payload"
                    )
                raise RuntimeError(
                    f"command {command_id!r} was already submitted with the same payload"
                )

        start_result: ConversationRunStartResult
        try:
            with self._session_factory.begin() as session:
                if task_id is None:
                    if workspace_id is None or session.get(WorkspaceModel, workspace_id) is None:
                        raise KeyError(workspace_id)
                    created_task = self._task.create(
                        workspace_id=workspace_id,
                        title=preview(input_text),
                        creation_command_id=command_id,
                        session=session,
                    )
                    task_id = created_task.id

                # 续接已有任务：校验任务存在，再创建 command 与 run；同一 Task 同时只允许
                # 一个 pending/running run，避免多个执行任务竞争同一份任务快照。
                task = self._task.ensure_task(session, task_id)
                task_id = task.id

                active_run = session.execute(
                    select(ConversationRunModel.id).where(
                        ConversationRunModel.task_id == task_id,
                        ConversationRunModel.status.in_(
                            (
                                ConversationRunStatus.PENDING.value,
                                ConversationRunStatus.RUNNING.value,
                            )
                        ),
                    )
                ).scalar_one_or_none()
                if active_run is not None:
                    raise ValueError(f"task {task_id} already has an active run")

                run = self._run.create(
                    task_id=task_id,
                    input_text=input_text,
                    agent_id="main_agent",
                    provider_id=provider_id,
                    model_name=model_name,
                    # image_paths=image_paths,
                    reasoning_effort=reasoning_effort,
                    session=session,
                )
                command = self._command.create(
                    task_id=task_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_hash=payload_hash,
                    run_id=run.id,
                )

                user_message, assistant_message = (
                    self._mutation_writer.create_run_messages_in_session(
                        session, task_id, run.id, run.input_text
                    )
                )
                state = _state_with_new_run(
                    initial_state or _empty_snapshot(),
                    run.id,
                    run.input_text,
                    user_message.id,
                    assistant_message.id,
                )
                self._snapshots.upsert_in_session(session, task_id, state)
                start_result = ConversationRunStartResult(command, run, task)
        except Exception as e:
            raise e
        self._snapshots.hydrate(task_id, state)
        return start_result


def _empty_snapshot() -> ConversationStateSnapshot:
    """返回新 Task 的空 ConversationState 快照。"""

    return {
        "messages": [],
        "run": {"runId": None, "status": "idle"},
        "error": None,
    }


def _state_with_new_run(
    base: ConversationStateSnapshot,
    run_id: int,
    input_text: str,
    user_message_id: int,
    assistant_message_id: int,
) -> ConversationStateSnapshot:
    """在已有 Task 快照尾部追加本次 user/assistant 消息与 pending run。"""

    state = copy.deepcopy(base)
    created_at = datetime.now(UTC).isoformat()
    state["messages"].extend(
        [
            {
                "id": f"message-{user_message_id}",
                "runId": run_id,
                "role": "user",
                "status": "complete",
                "endReason": None,
                "createdAt": created_at,
                "parts": [{"type": "text", "text": input_text, "status": "complete"}],
            },
            {
                "id": f"message-{assistant_message_id}",
                "runId": run_id,
                "role": "assistant",
                "status": "running",
                "endReason": None,
                "createdAt": created_at,
                "parts": [{"type": "text", "text": "", "status": "running"}],
            },
        ]
    )
    state["run"] = {"runId": run_id, "status": "pending"}
    state["error"] = None
    return state
