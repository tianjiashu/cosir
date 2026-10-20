"""Assistant Transport 命令接收与 Conversation Run 创建。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunInitializedEvent
from app.assistant_transport.request import AddMessageCommand, AssistantTransportRequest
from app.assistant_transport.request.command.ban_tools_command import BanToolsCommand
from app.assistant_transport.request.part import AssistantImagePart, AssistantTextPart
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    has_run,
)
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.runtime.execution_mode import ExecutionMode
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunRecord,
)
from app.models.conversation_run_options import ReasoningEffort
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.user_decision import UserDecision
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


def _build_ordered_display_text(
    parts: Sequence[AssistantTextPart | AssistantImagePart],
) -> str:
    """将 composer 的文本/图片顺序编码进输入消息的展示文本。"""

    image_token = Constant.Cosir.LOCAL_IMAGE_TOKEN
    emitted_image_ids = {
        match.group(1)
        for part in parts
        if isinstance(part, AssistantTextPart)
        for match in find_message_tokens(image_token, part.text)
    }
    segments: list[str] = []
    for part in parts:
        if isinstance(part, AssistantTextPart):
            segments.append(
                replace_message_tokens(Constant.Transport.HIDDEN_LOCAL_FILE_TOKEN, r"\1", part.text)
            )
        else:
            asset_id = part.image.removeprefix("cosir-attachment://")
            if asset_id in emitted_image_ids:
                continue
            segments.append(f"[[cosir-image:{asset_id}]]")
            emitted_image_ids.add(asset_id)
    return "\n".join(segments)


def _resumable_status(status: str) -> ConversationRunStatus | None:
    """返回显式续跑允许的来源：取消、失败、等待用户决定。"""

    if status in {ConversationRunStatus.CANCELLED.value, ConversationRunStatus.FAILED.value}:
        return ConversationRunStatus(status)
    if status == ConversationRunStatus.WAITING_FOR_INPUT.value:
        return ConversationRunStatus.WAITING_FOR_INPUT
    return None


@dataclass(frozen=True)
class ConversationRunStartResult:
    """命令接收回执；should_start 区分入队与启动，Run 身份在编辑和续跑时保持。"""

    run: ConversationRunRecord
    initial_state: ConversationStateSnapshot
    execution_mode: ExecutionMode = "fresh"
    should_start: bool = True


@dataclass(frozen=True)
class ConversationRunCommandInput:
    """描述一次 Run 请求接收的 Transport command。

    ``command_id`` 与 ``command_type`` 只用于保留请求命令的结构化边界；命令不会写入本地
    数据库，也不承担幂等身份或历史恢复职责。
    """

    command_id: str
    command_type: str


class ConversationRunCommandService:
    """Task 闸门内编排输入、编辑和恢复；执行器独立驱动，订阅不改变业务。"""

    def __init__(self) -> None:
        """初始化 Run 编排、Run 状态与 snapshot 持久化依赖。"""

        self._conversation_run = service_depends.get_conversation_run_service()
        self._run_state = service_depends.get_conversation_run_state_service()
        self._state = ConversationTaskStateService()
        self._task = service_depends.get_task_service()

    def build_command_inputs(
        self,
        request: AssistantTransportRequest,
    ) -> list[ConversationRunCommandInput]:
        """把 Transport 请求中的命令归一化为命令服务的输入信封列表。"""

        return [
            ConversationRunCommandInput(
                command_id=item.commandId,
                command_type=item.type,
            )
            for item in request.commands
        ]

    def build_run_command(
        self,
        request: AssistantTransportRequest,
        command: AddMessageCommand,
    ) -> ConversationRunCommand:
        """把 Transport 请求归一化为领域 Run 输入命令。

        ``display_text`` 复用既有 extra 文本编码，保持文字与图片的原始顺序，不新增数据库
        字段；``ban_tools`` 取自同一请求中的 ``BanToolsCommand``。
        """

        ban_command = next(
            (item for item in request.commands if isinstance(item, BanToolsCommand)),
            None,
        )
        display_text = _build_ordered_display_text(command.message.parts)
        image_asset_ids = list(
            dict.fromkeys(
                part.image.removeprefix("cosir-attachment://")
                for part in command.message.parts
                if isinstance(part, AssistantImagePart)
            )
        )
        return ConversationRunCommand(
            display_text=display_text,
            image_asset_ids=image_asset_ids,
            ban_tools=list(ban_command.payload.ban_tools) if ban_command is not None else [],
            attachments=[
                ConversationRunAttachmentInput(
                    id=attachment.id,
                    name=attachment.name,
                    content_type=attachment.contentType,
                    path=attachment.path,
                )
                for attachment in command.message.attachments
            ],
        )

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
            run」这条不变量：两条并发新建请求会被闸门依次放行，
            若省略本校验，同一 task 会同时存在两个 pending run 并被分别驱动。要下沉该
            不变量只能改成库级约束（``conversation_runs(task_id) WHERE status IN
            ('pending','running')`` 的部分唯一索引），而不是删掉校验。
        """

        if self._run_state.has_active_run(task_id, session=session):
            raise ValueError(f"task {task_id} already has an active run")

    def start_queued_run(self, task_id: int) -> ConversationRunStartResult | None:
        """前一驱动释放后在 Task 闸门内调用；只有正常完成才自动接下一批。"""
        from app.service.conversation_run.task_input_service import TaskInputService

        latest = self._task.get_latest_run(task_id)
        if latest is None or latest.status != "completed":
            return None
        pending = TaskInputService().pending(task_id)
        if not pending:
            return None
        payload = pending[0].payload
        with main_session_factory().begin() as session:
            self._assert_no_active_run(task_id, session)
            run = self._conversation_run.create_run(
                task_id=task_id,
                agent_id="main_agent",
                model_config_id=payload.model_config_id,
                queued_input=payload,
                session=session,
            )
        return self._activate_created_run(run)

    def submit_input(self, *, commands, model_config_id, reasoning_effort, task_id, run_command):
        """调用方持 Task 闸门；已有 Run 时只排队，暂停态不因消息到达自动恢复。"""
        latest = self._task.get_latest_run(task_id)
        if latest is not None and (
            latest.status in {"pending", "running", "waiting_for_input", "cancelled", "failed"}
            or service_depends.get_conversation_run_executor().has_execution(latest.id)
        ):
            from app.service.conversation_run.task_input_service import TaskInputService

            if model_config_id != latest.model_config_id:
                raise ValueError("当前 Run 尚未结束，不能切换模型")
            TaskInputService().enqueue(task_id, run_command, model_config_id, reasoning_effort)
            state = self._state.refresh_queued_inputs(task_id)
            return ConversationRunStartResult(run=latest, initial_state=state, should_start=False)
        return self.start_run(
            commands=commands,
            model_config_id=model_config_id,
            reasoning_effort=reasoning_effort,
            task_id=task_id,
            run_command=run_command,
        )

    def start_run(
        self,
        commands: Sequence[ConversationRunCommandInput],
        model_config_id: int | None,
        reasoning_effort: ReasoningEffort | None = None,
        task_id: int | None = None,
        run_command: ConversationRunCommand | None = None,
    ) -> ConversationRunStartResult:
        """接收一批 Transport command 并原子创建一个新 Run。

        参数:
            commands: 本次请求接收的全部命令；命令只参与本次请求，不落库。
            model_config_id: 模型厂商标识。
            reasoning_effort: 可选推理深度。
            task_id: 所属任务标识。
            run_command: 已由 Assistant Transport 转换的领域输入命令。

        返回:
            ``ConversationRunStartResult``：包含新建并完成初始装配的 Run。

        异常:
            RuntimeError: 快照重建后仍缺该 run（不变量被破坏）。
            ValueError: 任务已有其他 active run。
            KeyError: 任务或 run 不存在。

        副作用:
            在一个数据库事务内写入 Run；事务提交后若快照缺少该 run（与 canonical 分叉），
            先按 canonical 重建快照并发布 full 帧；此后任一步（投影、认领、快照重读）
            失败时，先把该 run 当场收敛为 ``cancelled``（end_reason=``run_setup_failed``）
            再原样抛出，不留下无执行器的 active run。
        """

        if task_id is None:
            raise ValueError("task_id is required for Assistant Transport runs")
        if run_command is None:
            raise ValueError("run_command is required for Assistant Transport runs")
        if not commands:
            raise ValueError("commands must not be empty")

        with main_session_factory().begin() as session:
            self._assert_no_active_run(task_id, session)
            run = self._conversation_run.create_run(
                task_id=task_id,
                agent_id="main_agent",
                model_config_id=model_config_id,
                reasoning_effort=reasoning_effort,
                session=session,
                run_command=run_command,
            )
        log.info(
            "task_input_enqueued",
            extra={
                "msg": "Run 与初始用户队列输入已提交",
                "data": {
                    "task_id": task_id,
                    "run_id": run.id,
                    "message_role": "user",
                },
            },
        )
        return self._activate_created_run(run)

    def _activate_created_run(self, run: ConversationRunRecord) -> ConversationRunStartResult:
        """首条输入与队列交接共用装配边界；提交后的装配失败必须收敛 Run。"""
        task_id = run.task_id
        try:
            service_depends.get_conversation_event_projector().process(
                RunInitializedEvent(
                    task_id=task_id,
                    run_id=run.id,
                    context_window_total=run.context_window_total,
                )
            )
            # 初始输入仍在 inbox，回执与订阅应立即显示它，不等待首个模型请求。
            self._state.publish_state(task_id, self._state.rebuild_state(task_id))
            running_run = self._run_state.claim_pending_run(run.id)
            if running_run is None:
                raise ValueError(f"run {run.id} is not editable in its current state")
            snapshot = self._state.get_state(task_id)
        except Exception:
            # 事务已提交，run 至少是 pending；此后任一步失败都必须当场收敛，否则留下
            # 无执行器的 active run 永久阻塞 task（启动 recover 只在重启时生效）。
            log.exception(
                "assistant_transport_run_setup_failed",
                extra={
                    "msg": "Run 已提交但请求内装配失败，当场收敛该 run 避免阻塞 task",
                    "data": {"task_id": task_id, "run_id": run.id},
                },
            )
            self._converge_failed_setup(run.id)
            raise
        return ConversationRunStartResult(
            run=running_run,
            initial_state=snapshot,
            execution_mode="fresh",
        )

    def edit_input(
        self,
        task_id: int,
        run_id: int,
        message_id: str,
        run_command: ConversationRunCommand,
        model_config_id: int,
        reasoning_effort: ReasoningEffort | None,
    ) -> ConversationRunStartResult:
        """消息级编辑：保留 Run 身份和目标消息之前的 context。"""
        from app.service.conversation_run.task_input_service import TaskInputService

        latest = self._task.get_latest_run(task_id)
        if latest is None or latest.id != run_id:
            raise ValueError("只能编辑当前任务最新的用户消息")
        should_start = TaskInputService().edit_latest(
            task_id, run_id, message_id, run_command, model_config_id, reasoning_effort
        )
        if should_start:
            state = self._state.rebuild_state(task_id)
            self._state.publish_state(task_id, state)
        else:
            state = self._state.refresh_queued_inputs(task_id)
        return ConversationRunStartResult(
            run=self._conversation_run.get_run(run_id),
            initial_state=state,
            execution_mode="resume",
            should_start=should_start,
        )

    def resume_run(
        self,
        task_id: int,
        run_id: int,
        *,
        expected_status: ConversationRunStatus | None = None,
        user_decision: UserDecision | None = None,
    ) -> ConversationRunStartResult:
        """验证最新 Run 与待决请求，先持久化决定及状态迁移，再交执行器启动。"""
        execution_mode: ExecutionMode = "resume"
        latest_run = self._task.get_latest_run(task_id)
        if latest_run is None or latest_run.id != run_id:
            raise ValueError(f"run {run_id} does not belong to task {task_id}")
        current_status = expected_status or _resumable_status(latest_run.status)
        if current_status is None or latest_run.status != current_status.value:
            raise ValueError(f"run {run_id} is not resumable from status {latest_run.status}")
        if current_status is ConversationRunStatus.WAITING_FOR_INPUT:
            from app.core.workflow.node_helper.user_input_projection import extract_request

            continuation = latest_run.execution_data.continuation
            request = extract_request(continuation.observations) if continuation else None
            if (
                request is None
                or user_decision is None
                or request.request_id != user_decision.request_id
                or not request.accepts(user_decision.kind)
            ):
                raise ValueError("等待恢复需要当前请求的有效决定")
        elif user_decision is not None:
            raise ValueError("当前 Run 不接受用户决定")
        state = self._ensure_run_visible(task_id, run_id)
        if state["current_run_id"] != run_id:
            raise ValueError(f"run {run_id} is not the current conversation_run run")
        try:
            if user_decision is None:
                resumed = self._run_state.resume_cancelled_run(run_id)
            else:
                execution_mode = "resume_with_input"
                resumed = self._run_state.resume_waiting_run(run_id, user_decision=user_decision)
            if resumed is None:
                raise ValueError(f"run {run_id} is no longer resumable")
            state = self._state.get_state(task_id)
        except Exception:
            log.exception(
                "assistant_transport_resume_setup_failed",
                extra={
                    "msg": "续跑装配失败，已收敛该 run，避免留下无执行器的 active run",
                    "data": {"task_id": task_id, "run_id": run_id},
                },
            )
            self._converge_failed_setup(run_id, end_reason="resume_setup_failed")
            raise
        return ConversationRunStartResult(
            run=resumed,
            initial_state=state,
            execution_mode=execution_mode,
        )

    def _ensure_run_visible(self, task_id: int, run_id: int) -> ConversationStateSnapshot:
        """返回含指定 run 的快照；快照缺失该 run（与 canonical 分叉）时按 canonical 重建。

        快照是进程内 working copy，允许短暂滞后；但当它**完全没有**某条 canonical 确实存在
        的 run 时，一切基于快照的存在性/归属校验都会永久失败，且会连锁出两个症状：

        - 新 run 的 ``RunInitializedEvent`` 被陈旧事件防护丢弃（快照最后一个 run 仍是非终态），
          随后 ``claim_pending_run`` 发布 RUNNING 时因找不到该 run 抛 ``KeyError``，该对话
          **每次发送都 500**；
        - ``resume`` 因 ``current_run_id`` 不匹配而返回「不是当前 run」，且无法自愈。

        进程内没有其它恢复入口（``get_state`` 见到已物化副本不重建），因此这里在编排早期
        显式复核并重建，让对话当场恢复可用。

        参数:
            task_id: 目标任务标识。
            run_id: 必须出现在快照里的 Conversation Run 标识。

        返回:
            含该 run 的快照：正常路径是当前副本，分叉路径是重建后的新副本。

        异常:
            RuntimeError: 重建后仍不含该 run——canonical 也没有该 run，说明创建/重置事务
                未生效，属不变量被破坏；立即失败，不继续认领以免留下无骨架的 active run。

        副作用:
            仅在分叉时卸载并重建该 task 的进程内快照，并向 subscriber 发布一个 full 帧让
            客户端收敛；正常路径只做一次只读复核。
        """

        snapshot = self._state.get_state(task_id)
        if has_run(snapshot, run_id):
            return snapshot
        log.warning(
            "assistant_transport_snapshot_diverged_rebuilding",
            extra={
                "msg": "快照缺少该 run，疑似与 canonical 分叉，按 canonical 重建",
                "data": {"task_id": task_id, "run_id": run_id},
            },
        )
        rebuilt: ConversationStateSnapshot = self._state.rebuild_state(task_id)
        if not has_run(rebuilt, run_id):
            raise RuntimeError(f"run {run_id} is absent from both snapshot and canonical rebuild")
        self._state.publish_state(task_id, rebuilt)
        return rebuilt

    def _converge_failed_setup(self, run_id: int, *, end_reason: str = "run_setup_failed") -> None:
        """run 事务已提交、但请求内后续步骤失败时的当场收敛。

        负责把本次请求碰过的 run（认领前 ``pending``、认领后 ``running``）统一收敛为
        ``cancelled``：``cancel_run_if_running`` 的条件更新覆盖两种 active 状态，对未认领
        命中或已落终态的 run 是 no-op，不构成第二套状态写入者。选择 ``cancelled`` 而非
        ``failed`` 是为了保留用户通过 resume 重试的能力。

        参数:
            run_id: 待收敛的 Conversation Run 标识。
            end_reason: 写入终态的稳定原因标识（需为合法标识符以匹配受控错误契约）。

        返回:
            无。

        异常:
            无。收敛自身失败只记日志、不抛出，不覆盖调用方的原始异常；残余窗口由
            启动期 ``recover_orphaned_runs`` 兜底。

        副作用:
            条件更新 run 为 ``cancelled`` 并随之发布状态事件；写日志。
        """

        try:
            self._run_state.cancel_run_if_running(run_id, end_reason=end_reason)
        except Exception:
            log.exception(
                "assistant_transport_run_setup_converge_failed",
                extra={
                    "msg": "run 装配失败后的当场收敛未落库，依赖下次启动 recover 兜底",
                    "data": {"run_id": run_id},
                },
            )
