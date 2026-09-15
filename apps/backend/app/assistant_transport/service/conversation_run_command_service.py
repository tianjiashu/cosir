"""Assistant Transport command 幂等占用与 Conversation Run 创建。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunInitializedEvent
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
)
from app.config.logging.logger import log
from app.core.runtime.execution_mode import ExecutionMode
from app.models import ConversationRunCommand, ConversationRunRecord
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
        self._state = ConversationTaskStateService()
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
            raise RuntimeError(f"command {command_id!r} already exists with a different payload")
        if existing.run_id is None:
            raise RuntimeError(f"command {command_id!r} exists without a bound run")
        run = self._conversation_run.get_run(existing.run_id)
        return ConversationRunStartResult(
            command=existing,
            run=run,
            initial_state=self._state.get_state(task_id),
            created=False,
            execution_mode="fresh",
            mode=mode,
        )

    def resolve_existing_command(
        self,
        task_id: int,
        command_id: str,
        payload_hash: str,
        mode: RunCommandMode,
    ) -> ConversationRunStartResult | None:
        """在解析附件路径前检查幂等命令，避免重复请求依赖源文件仍存在。

        该只读查询用于 Assistant Transport 的早期幂等短路；真正创建/编辑 Run 时仍会在
        task 操作闸门内再次检查，防止查询与写入之间出现竞态。
        """
        return self._resolve_existing_command(task_id, command_id, payload_hash, mode)

    def _assert_no_active_run(self, task_id: int, session: Session | None = None) -> None:
        """任务已有 active run 时拒绝新的 run 占用请求。

        参数:
            task_id: 目标任务标识。
            session: 调用方已开启的事务 session（校验与创建 run 在同一事务内完成）。

        返回:
            无。

        异常:
            ValueError: 该 task 已存在 ``pending`` / ``running`` 的 run，由 API 层翻译为
                ``RUN_START_CONFLICT``(409)。

        副作用:
            无（仅事务内只读查询）。

        并发:
            **本校验不是 Task 操作闸门的重复，禁止以"task 锁已保证互斥"为由删除。**
            Task 操作闸门只把「创建 run」串行化，并不表达「一个 task 同时只允许一个 active
            run」这条不变量：两条 ``command_id`` 不同的并发新建请求会被闸门依次放行，
            若省略本校验，同一 task 会同时存在两个 pending run 并被分别驱动。要下沉该
            不变量只能改成库级约束（``conversation_runs(task_id) WHERE status IN
            ('pending','running')`` 的部分唯一索引），而不是删掉校验。
        """

        if self._conversation_run.have_run_in_runing(task_id, session=session):
            raise ValueError(f"task {task_id} already has an active run")

    def start_or_attach(
        self,
        command_id: str,
        command_type: str,
        payload_hash: str,
        provider_id: int | None,
        model_name: str | None,
        reasoning_effort: str | None = None,
        task_id: int | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunStartResult:
        """创建新 run，或为相同 command_id 返回原 run。

        参数:
            command_id: Assistant Transport 命令幂等标识。
            command_type: Transport 命令类型。
            payload_hash: 命令业务载荷指纹。
            provider_id: 模型厂商标识。
            model_name: 模型名称。
            reasoning_effort: 可选推理深度。
            task_id: 所属任务标识。
            run_command: 已由 Assistant Transport 转换的领域输入命令。

        返回:
            ``created=True`` 表示本次创建了 run；``created=False`` 表示应重新订阅已有 run。

        异常:
            RuntimeError: command_id 已存在但 payload 冲突或记录未绑定 run。
            ValueError: 任务已有其他 active run。
            KeyError: 任务或 run 不存在。

        副作用:
            首次调用在一个数据库事务内写入 command、run 和 context baseline；重复调用只读
            已有 command/run，不创建第二个 run。
        """

        if task_id is None:
            raise ValueError("task_id is required for Assistant Transport runs")
        if run_command is None:
            raise ValueError("run_command is required for Assistant Transport runs")

        existing_result = self._resolve_existing_command(
            task_id, command_id, payload_hash, "new"
        )
        if existing_result is not None:
            return existing_result

        with main_session_factory().begin() as session:
            self._assert_no_active_run(task_id, session)
            run = self._conversation_run.create_run(
                task_id=task_id,
                agent_id="main_agent",
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                session=session,
                run_command=run_command,
            )
            command = self._command.create(
                task_id=task_id,
                command_id=command_id,
                command_type=command_type,
                payload_hash=payload_hash,
                run_id=run.id,
                session=session,
            )
        log.info(
            "context_message_persisted",
            extra={
                "msg": "Run 初始 user context 已提交",
                "data": {
                    "task_id": task_id,
                    "run_id": run.id,
                    "message_type": "HumanMessage",
                },
            },
        )
        service_depends.get_conversation_event_projector().process(
            RunInitializedEvent(
                task_id=task_id,
                run_id=run.id,
                image_paths=run.image_paths or [],
                file_attachments=(
                    [
                        {
                            "id": attachment["id"],
                            "name": attachment["name"],
                            "content_type": attachment["content_type"],
                            "path": attachment["path"],
                        }
                        for attachment in run.extra.attachments
                    ]
                    if run.extra is not None
                    else []
                ),
                include_text_part=bool(
                    (
                        run.extra.display_text
                        if run.extra is not None
                        else run.input_text
                    ).strip()
                ),
            )
        )
        running_run = self._conversation_run.claim_pending_run(run.id)
        if running_run is None:
            raise ValueError(f"run {run.id} is not editable in its current state")
        snapshot = self._state.get_state(task_id)
        return ConversationRunStartResult(
            command=command,
            run=running_run,
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
        task_id: int,
        run_id: int,
        provider_id: int | None,
        model_name: str | None,
        reasoning_effort: str | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunStartResult:
        """原地编辑当前 run 的最后一条用户消息并重置执行基线。

        ``run_id`` 必须是 task 最近 run，且 canonical context 中必须存在该 run 的 user 消息。
        旧 run 的 context entries 按 ``ContextEntry.run_id`` 删除，run 保留原 id，
        但会换用新的 checkpoint thread；Assistant UI ``sourceId`` 不参与本用例。
        """

        if run_command is None:
            raise ValueError("run_command is required for Assistant Transport runs")
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
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                session=session,
                run_command=run_command,
            )
            if reset is None:
                raise ValueError(f"run {latest_run.id} is not editable in its current state")
            self._context.delete_by_run_id(task_id, latest_run.id, session=session)
            command = self._command.create(
                task_id=task_id,
                command_id=command_id,
                command_type=command_type,
                payload_hash=payload_hash,
                run_id=latest_run.id,
                session=session,
            )
        log.info(
            "context_message_persisted",
            extra={
                "msg": "Run 初始 user context 已提交",
                "data": {
                    "task_id": task_id,
                    "run_id": reset.id,
                    "message_type": "HumanMessage",
                },
            },
        )
        service_depends.get_conversation_event_projector().process(
            RunInitializedEvent(
                task_id=task_id,
                run_id=reset.id,
                image_paths=reset.image_paths or [],
                file_attachments=(
                    [
                        {
                            "id": attachment["id"],
                            "name": attachment["name"],
                            "content_type": attachment["content_type"],
                            "path": attachment["path"],
                        }
                        for attachment in reset.extra.attachments
                    ]
                    if reset.extra is not None
                    else []
                ),
                include_text_part=bool(
                    (
                        reset.extra.display_text
                        if reset.extra is not None
                        else reset.input_text
                    ).strip()
                ),
                replace_existing=True,
            )
        )
        snapshot: ConversationStateSnapshot = self._state.get_state(task_id)
        self._state.publish_state(task_id, snapshot)
        return ConversationRunStartResult(
            command=command,
            run=reset,
            initial_state=snapshot,
            created=True,
            execution_mode="fresh",
            mode="edit",
        )


    def resume_latest_run(self, task_id: int, run_id: int) -> ConversationRunStartResult:
        """校验 task 最近 run 并返回业务续跑的执行结果。

        该用例只负责领域身份和持久状态资格判断；真正的 executor 启动由 API 编排层
        完成，Transport 层只负责随后建立 snapshot response。

        执行顺序刻意把所有可能失败的只读步骤放在唯一写操作之前：先校验 run 身份与状态、
        snapshot 归属与用户消息、驱动命令可读，再原子恢复 run 状态。否则一旦写操作之后才
        暴露错误，run 会停在 ``running`` 却没有执行器，后续 resume 会被状态校验永久拒绝。
        写操作之后的 snapshot 重读若失败，会补偿收敛该 run。

        参数:
            task_id: 目标任务标识。
            run_id: 待续跑的 Conversation Run 标识。

        返回:
            ``created=True``、``execution_mode="resume"`` 的启动结果；``command`` 为该 run
            最近一次提交的命令记录（一个 run 可绑定多条 command）。

        异常:
            ValueError: run 不是 task 最近 run、不是 ``cancelled``、snapshot 归属失效、
                缺少用户消息，或恢复时已被其它路径落定终态。

        副作用:
            事务性把 ``cancelled`` run 恢复为 ``running``（清空旧终态字段）并发布 RUNNING
            状态事件；补偿路径会把该 run 收敛回终态，避免留下无执行器的 active run。
        """

        latest_run = self._task.get_latest_run(task_id)
        if latest_run is None or latest_run.id != run_id or latest_run.status != "cancelled":
            raise ValueError(f"run {run_id} is not resumable")
        state = self._state.get_state(task_id)
        if state["current_run_id"] != run_id:
            raise ValueError(f"run {run_id} is not the current task run")
        if not any(
                message["role"] == "user"
                for run in state["runs"]
                if run["runId"] == run_id
                for message in run["messages"]
        ):
            raise ValueError(f"run {run_id} has no user message")
        # 驱动命令读取是最后一个只读步骤：一个 run 可绑定多条 command（同轮编辑重跑会
        # 追加一条），这里取最近一条。必须在写操作之前完成，避免失败时留下已恢复的 run。
        command = self._command.get_by_run(run_id)
        resumed = self._conversation_run.resume_cancelled_run(run_id)
        if resumed is None:
            raise ValueError(f"run {run_id} is no longer resumable")
        try:
            state = self._state.get_state(task_id)
        except Exception:
            # run 已置 running，但本次续跑不会启动执行器；收敛回终态，让用户可重试，
            # 而不是留下一个永远无法 resume 的 active run。
            log.exception(
                "assistant_transport_resume_setup_failed",
                extra={
                    "msg": "续跑读取快照失败，已收敛该 run，避免留下无执行器的 active run",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            self._conversation_run.cancel_run_if_running(
                run_id, end_reason="resume_setup_failed"
            )
            raise
        return ConversationRunStartResult(
            command=command,
            run=resumed,
            initial_state=state,
            created=True,
            execution_mode="resume",
            mode="resume",
        )

