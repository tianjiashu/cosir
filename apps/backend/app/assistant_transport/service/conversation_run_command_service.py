"""Assistant Transport command 幂等占用与 Conversation Run 创建。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.core.runtime.execution_mode import ExecutionMode
from app.models import ConversationRunRecord
from app.models.conversation_command_record import ConversationCommandRecord
from app.service import depends as service_depends
from app.service.task.conversation_task_context_service import ConversationTaskContextService
from app.storage.store_engines import main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

RunCommandMode = Literal["new", "edit", "resume"]


@dataclass(frozen=True)
class ConversationRunStartResult:
    """表示一次 Run 命令分类、创建或幂等重连的结果。

    ``mode`` 表示业务语义，``execution_mode`` 表示 AgentRuntime 的执行方式。
    编辑重跑因此是 ``mode="edit"`` 与 ``execution_mode="fresh"`` 的组合，且保留
    原 Conversation Run id。
    """

    command: ConversationCommandRecord | None
    run: ConversationRunRecord
    initial_state: ConversationStateSnapshot
    created: bool
    execution_mode: ExecutionMode = "fresh"
    mode: RunCommandMode = "new"


class ConversationRunCommandService:
    """在任务边界内原子创建 run，或返回 command 已绑定的原 run。"""

    def __init__(self) -> None:
        """初始化 command、run 和 snapshot 持久化依赖。"""

        self._command = service_depends.get_conversation_command_crud()
        self._conversation_run = service_depends.get_conversation_run_service()
        self._snapshots = ConversationTaskSnapshotService()
        self._context = ConversationTaskContextService()
        self._task = service_depends.get_task_service()

    def _resolve_existing_command(
        self,
        task_id: int,
        command_id: str,
        payload_hash: str,
        mode: RunCommandMode,
    ) -> ConversationRunStartResult | None:
        """查找并校验已存在的同 command_id 命令，命中则返回既有 run 的续订结果。

        把「幂等命令已存在」分支的两条子逻辑（载荷校验、绑定 run 加载与结果装配）
        收敛到一处：先比对 ``payload_hash`` 防重放冲突，再确认命令已绑定 ``run_id``，
        最后读回 run 并装配 ``created=False`` 的续订结果。命令不存在时返回 ``None``，
        交由调用方继续走新建 run 事务。

        参数:
            task_id: 任务标识。
            command_id: 幂等命令标识。
            payload_hash: 本次请求的 payload 指纹。
            mode: 本次请求归一化后的业务模式。

        返回:
            命中且校验通过的 ``ConversationRunStartResult``（created=False）；
            该 command_id 尚不存在时返回 ``None``。

        异常:
            RuntimeError: 已存在命令的 payload 与本次不一致，或已存在命令未绑定 run。
            KeyError: 命令所绑定的 run 不存在。

        副作用:
            无（仅读取）。
        """

        existing = self._command.get(task_id, command_id)
        if existing is None:
            return None
        if existing.payload_hash != payload_hash:
            raise RuntimeError(
                f"command {command_id!r} already exists with a different payload"
            )
        if existing.run_id is None:
            raise RuntimeError(f"command {command_id!r} exists without a bound run")
        run = self._conversation_run.get_run(existing.run_id)
        return ConversationRunStartResult(
            command=existing,
            run=run,
            initial_state=self._snapshots.ensure_state_snapshot(task_id),
            created=False,
            execution_mode="fresh",
            mode=mode,
        )

    def _assert_no_active_run(self, task_id: int, session: Session | None = None) -> None:
        """任务已有 active run 时拒绝新的 run 占用请求。"""

        if self._conversation_run.have_run_in_runing(task_id, session=session):
            raise ValueError(f"task {task_id} already has an active run")

    @contextmanager
    def _task_run_operation(self, task_id: int) -> Iterator[None]:
        """在 task 运行时空间内执行一个带超时的独占操作。"""

        task_space = task_runtime_spaces.get_or_create(task_id)
        with task_space.operation(timeout=10):
            yield

    def start_or_attach(
        self,
        command_id: str,
        command_type: str,
        payload_hash: str,
        input_text: str,
        provider_id: int | None,
        model_name: str | None,
        reasoning_effort: str | None = None,
        task_id: int | None = None,
    ) -> ConversationRunStartResult:
        """创建新 run，或为相同 command_id 返回原 run。

        参数:
            command_id: Assistant Transport 命令幂等标识。
            command_type: Transport 命令类型。
            payload_hash: 命令业务载荷指纹。
            input_text: 用户输入文本。
            provider_id: 模型厂商标识。
            model_name: 模型名称。
            reasoning_effort: 可选推理深度。
            task_id: 所属任务标识。

        返回:
            ``created=True`` 表示本次创建了 run；``created=False`` 表示应重新订阅已有 run。

        异常:
            RuntimeError: command_id 已存在但 payload 冲突或记录未绑定 run。
            ValueError: 任务已有其他 active run。
            KeyError: 任务或 run 不存在。

        副作用:
            首次调用在一个数据库事务内写入 command、run 和 snapshot baseline；重复调用只读
            已有 command/run，不创建第二个 run。
        """

        if task_id is None:
            raise ValueError("task_id is required for Assistant Transport runs")

        with self._task_run_operation(task_id):
            existing_result = self._resolve_existing_command(
                task_id, command_id, payload_hash, "new"
            )
            if existing_result is not None:
                return existing_result

            with main_session_factory().begin() as session:
                self._assert_no_active_run(task_id, session)
                run = self._conversation_run.create_run(
                    task_id=task_id,
                    input_text=input_text,
                    agent_id="main_agent",
                    provider_id=provider_id,
                    model_name=model_name,
                    reasoning_effort=reasoning_effort,
                    session=session,
                )
                command = self._command.create(
                    task_id=task_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_hash=payload_hash,
                    run_id=run.id,
                    session=session,
                )
                snapshot = self._snapshots.ensure_state_snapshot(task_id, session)
            return ConversationRunStartResult(
                command=command,
                run=run,
                initial_state=snapshot,
                created=True,
                execution_mode="fresh",
                mode="new",
            )

    def edit_or_restart(
        self,
        command_id: str,
        command_type: str,
        payload_hash: str,
        input_text: str,
        task_id: int,
        run_id: int,
        provider_id: int | None,
        model_name: str | None,
        reasoning_effort: str | None = None,
    ) -> ConversationRunStartResult:
        """原地编辑当前 run 的最后一条用户消息并重置执行基线。

        ``run_id`` 必须是 task 最近 run，且 snapshot 中必须存在该 run 的 user 消息。
        旧 run 的 context entries 按 ``ContextEntry.run_id`` 删除，run 保留原 id，
        但会换用新的 checkpoint thread；Assistant UI ``sourceId`` 不参与本用例。
        """

        with self._task_run_operation(task_id):
            latest_run = self._task.get_latest_run(task_id)
            if latest_run is None or latest_run.id != run_id:
                raise ValueError(f"run {run_id} does not belong to task {task_id}")

            existing_result = self._resolve_existing_command(
                task_id, command_id, payload_hash, "edit"
            )
            if existing_result is not None:
                return existing_result


            with main_session_factory().begin() as session:
                self._assert_no_active_run(task_id, session)
                reset = self._conversation_run.reset_run_for_edit(
                    latest_run.id,
                    input_text,
                    provider_id,
                    model_name,
                    reasoning_effort,
                    session=session,
                )
                if reset is None:
                    raise ValueError(f"run {latest_run.id} is not editable in its current state")
                self._context.delete_by_run_id(task_id, latest_run.id, session=session)
                snapshot = self._snapshots.reset_run_for_edit(
                    task_id, latest_run.id, input_text, session=session
                )
                command = self._command.create(
                    task_id=task_id,
                    command_id=command_id,
                    command_type=command_type,
                    payload_hash=payload_hash,
                    run_id=latest_run.id,
                    session=session,
                )
            self._snapshots.publish_committed_snapshot(task_id, snapshot)
            return ConversationRunStartResult(
                command=command,
                run=reset,
                initial_state=snapshot,
                created=True,
                execution_mode="fresh",
                mode="edit",
            )

    def resume_latest_run(
        self, task_id: int, run_id: int
    ) -> ConversationRunStartResult:
        """校验 task 最近 run 并返回业务续跑的执行结果。

        该用例只负责领域身份和持久状态资格判断；真正的 executor 启动由 API 编排层
        完成，Transport 层只负责随后建立 snapshot response。
        """

        with self._task_run_operation(task_id):
            latest_run = self._task.get_latest_run(task_id)
            if (
                latest_run is None
                or latest_run.id != run_id
                or latest_run.status != "cancelled"
            ):
                raise ValueError(f"run {run_id} is not resumable")
            state = self._snapshots.ensure_state_snapshot(task_id)
            if state["run"]["runId"] != run_id:
                raise ValueError(f"run {run_id} snapshot is stale")
            if not any(
                message.get("runId") == run_id and message.get("role") == "user"
                for message in state["messages"]
            ):
                raise ValueError(f"run {run_id} has no user message")
            resumed = self._conversation_run.resume_cancelled_run(run_id)
            if resumed is None:
                raise ValueError(f"run {run_id} is no longer resumable")
            state = self._snapshots.ensure_state_snapshot(task_id)
            return ConversationRunStartResult(
                command=self._command.get_by_run(run_id),
                run=resumed,
                initial_state=state,
                created=True,
                execution_mode="resume",
                mode="resume",
            )
