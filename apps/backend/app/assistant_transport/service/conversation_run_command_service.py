"""Assistant Transport 命令接收与 Conversation Run 创建。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunInitializedEvent
from app.assistant_transport.request import AddMessageCommand, AssistantTransportRequest
from app.assistant_transport.request.command.ban_tools_command import BanToolsCommand
from app.assistant_transport.request.command.propose_agent_configuration_command import (
    ProposeAgentConfigurationCommand,
)
from app.assistant_transport.request.command.propose_agent_team_configuration_command import (
    ProposeAgentTeamConfigurationCommand,
)
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
from app.core.tools.schemas.user_decision import UserDecision
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunRecord,
)
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory


def _build_ordered_display_text(
        parts: Sequence[AssistantTextPart | AssistantImagePart],
) -> str:
    """将 composer 的文本/图片顺序编码进既有 Run 展示文本值。"""

    image_token = Constant.Cosir.LOCAL_IMAGE_TOKEN
    emitted_image_ids = {
        image_id
        for part in parts
        if isinstance(part, AssistantTextPart)
        for image_id in image_token.findall(
            Constant.Transport.HIDDEN_LOCAL_FILE_TOKEN.sub(r"\1", part.text)
        )
    }
    segments: list[str] = []
    for part in parts:
        if isinstance(part, AssistantTextPart):
            segments.append(Constant.Transport.HIDDEN_LOCAL_FILE_TOKEN.sub(r"\1", part.text))
        else:
            asset_id = part.image.removeprefix("cosir-attachment://")
            if asset_id in emitted_image_ids:
                continue
            segments.append(f"[[cosir-image:{asset_id}]]")
            emitted_image_ids.add(asset_id)
    return "\n".join(segments)


def _resumable_status(status: str) -> ConversationRunStatus | None:
    """把 Run 的持久化状态映射为允许的续跑源状态。

    只有这两种状态能续跑同一 checkpoint，差别在语义来源：``cancelled`` 是「被取消 / 崩溃收敛
    后由用户继续」，``waiting_for_input`` 是「等待用户决定后带决定继续」。其他状态（含终态
    失败与已完成）都不允许续跑。

    参数:
        status: ``conversation_runs.status`` 的持久化值。

    返回:
        对应的 ``ConversationRunStatus``；不可续跑时为 ``None``。

    异常:
        无。

    副作用:
        无。
    """

    if status == ConversationRunStatus.CANCELLED.value:
        return ConversationRunStatus.CANCELLED
    if status == ConversationRunStatus.WAITING_FOR_INPUT.value:
        return ConversationRunStatus.WAITING_FOR_INPUT
    return None


@dataclass(frozen=True)
class ConversationRunStartResult:
    """表示一次 Run 命令分类与执行准备结果。

    ``execution_mode`` 表示 AgentRuntime 的执行方式；编辑重跑由调用方先删除被编辑的
    Run、再走新建路径实现，因此其结果就是普通新建结果（``execution_mode="fresh"``，
    ``run`` 与被编辑的 Run 不同）。

    ``user_decision`` 是本次续跑携带的用户结构化决定（human-in-the-loop）；创建 / 编辑
    路径恒为 ``None``。它随结果一并交给调用方，避免决定经过进程内旁路或二次查询传递。
    """

    run: ConversationRunRecord
    initial_state: ConversationStateSnapshot
    execution_mode: ExecutionMode = "fresh"
    user_decision: UserDecision | None = None


@dataclass(frozen=True)
class ConversationRunCommandInput:
    """描述一次 Run 请求接收的 Transport command。

    ``command_id`` 与 ``command_type`` 只用于保留请求命令的结构化边界；命令不会写入本地
    数据库，也不承担幂等身份或历史恢复职责。
    """

    command_id: str
    command_type: str


class ConversationRunCommandService:
    """在任务边界内接收 Transport command 并原子创建、删除（编辑重跑）或恢复 Run。"""

    def __init__(self) -> None:
        """初始化 Run 编排、Run 状态与 snapshot 持久化依赖。"""

        self._conversation_run = service_depends.get_conversation_run_service()
        self._run_state = service_depends.get_conversation_run_state_service()
        self._state = ConversationTaskStateService()
        self._task = service_depends.get_task_service()

    def build_command_inputs(
            self, request: AssistantTransportRequest,
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
        字段；``ban_tools`` 与 ``propose_agent_configuration`` 取自同一请求中的兄弟命令
        （``BanToolsCommand`` / ``ProposeAgentConfigurationCommand``）。
        """

        ban_command = next(
            (item for item in request.commands if isinstance(item, BanToolsCommand)),
            None,
        )
        proposal_command = next(
            (
                item
                for item in request.commands
                if isinstance(item, ProposeAgentConfigurationCommand)
            ),
            None,
        )
        team_proposal_command = next(
            (
                item
                for item in request.commands
                if isinstance(item, ProposeAgentTeamConfigurationCommand)
            ),
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
            propose_agent_configuration=proposal_command is not None,
            propose_agent_team_configuration=team_proposal_command is not None,
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

    def start_run(
            self,
            commands: Sequence[ConversationRunCommandInput],
            model_config_id: int | None,
            reasoning_effort: str | None = None,
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
        try:
            service_depends.get_conversation_event_projector().process(
                RunInitializedEvent(
                    task_id=task_id,
                    run_id=run.id,
                    context_window_total=self._task.get_context_window_total(task_id),
                )
            )
            self._ensure_run_visible(task_id, run.id)
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

    def delete_edited_run(self, task_id: int, run_id: int) -> None:
        """删除被编辑的最近 Run，为「编辑重跑」腾出 task 的 active 槽位。

        编辑重跑复用新建路径：本方法只删除被编辑的 Run 及其全部会话事实（context、
        checkpoint、终端元数据，并置空引用它的 ``tasks.parent_run_id``），随后由调用方
        （Assistant Transport）继续走 ``start_run`` 创建一条全新 Run。``run_id`` 必须是
        该 task 的最近 Run。

        参数:
            task_id: 所属任务标识。
            run_id: 被编辑的 Conversation Run 标识（必须是该 task 的最近 run）。

        返回:
            无。

        异常:
            ValueError: ``run_id`` 不是该 task 最近 run，或 task 已有 active run
                （由 API 层翻译为可重试的 409）。
            KeyError: task 或 run 不存在。

        副作用:
            删除该 Run 的 context / checkpoint / 终端元数据；并按 canonical 重建 Transport
            进程内快照，丢弃仍含该 Run 的 working copy。客户端可见性由随后新建 Run 的响应
            首帧（full）收敛，本方法不发布帧。
        """

        latest_run = self._task.get_latest_run(task_id)
        if latest_run is None or latest_run.id != run_id:
            raise ValueError(f"run {run_id} does not belong to task {task_id}")
        # 显式用 ValueError 判 active：``delete_run_locked`` 抛的
        # ``RunDeletionConflictError`` 是 RuntimeError，会绕过 API 的 409 映射变成 500。
        self._assert_no_active_run(task_id)
        # 调用方（Assistant Transport）已持有 task 操作闸门；该闸门是非可重入的
        # ``threading.Lock``，因此用已持锁变体删除，不能再进入 ``delete_run``。
        self._task.delete_run_locked(task_id, run_id)
        # 进程内 working copy 仍含被删 Run：按 canonical 重建、丢弃它，让随后的 start_run
        # 把新 Run 投影进这份干净副本。
        self._state.rebuild_state(task_id)

    def resume_run(
            self,
            task_id: int,
            run_id: int,
            *,
            expected_status: ConversationRunStatus | None = None,
            user_decision: UserDecision | None = None,
    ) -> ConversationRunStartResult:
        """校验最近 Run 的身份和状态，迁移为 running 并返回 checkpoint 续跑结果。

        参数:
            task_id: 目标任务标识。
            run_id: 待续跑的 Run 标识。
            expected_status: 允许的续跑源状态；``None`` 表示按 Run 当前持久化状态自动判定
                （``cancelled`` 与 ``waiting_for_input`` 都续跑同一 checkpoint，差别只在
                语义来源）。显式传入用于「只允许等待态恢复」这类收窄场景。
            user_decision: 本次续跑携带的用户结构化决定（human-in-the-loop）；随结果原样
                交给调用方，由执行器透传进图。

        异常:
            ValueError: Run 不存在、不属于该 task、当前状态不可续跑，或缺少用户消息。
        """
        execution_mode: ExecutionMode = "resume"
        latest_run = self._task.get_latest_run(task_id)
        if latest_run is None or latest_run.id != run_id:
            raise ValueError(f"run {run_id} does not belong to task {task_id}")
        current_status = expected_status or _resumable_status(latest_run.status)
        if current_status is None or latest_run.status != current_status.value:
            raise ValueError(f"run {run_id} is not resumable from status {latest_run.status}")
        state = self._ensure_run_visible(task_id, run_id)
        if state["current_run_id"] != run_id:
            raise ValueError(f"run {run_id} is not the current conversation_run run")
        if not any(
                message["role"] == "user"
                for run in state["runs"]
                if run["runId"] == run_id
                for message in run["messages"]
        ):
            raise ValueError(f"run {run_id} has no user message")
        try:
            if current_status is ConversationRunStatus.CANCELLED:
                resumed = self._run_state.resume_cancelled_run(run_id)
            else:
                execution_mode = "resume_with_input"
                resumed = self._run_state.resume_waiting_run(run_id)
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
            user_decision=user_decision,
        )

    def _ensure_run_visible(
            self, task_id: int, run_id: int
    ) -> ConversationStateSnapshot:
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
            raise RuntimeError(
                f"run {run_id} is absent from both snapshot and canonical rebuild"
            )
        self._state.publish_state(task_id, rebuilt)
        return rebuilt

    def _converge_failed_setup(
            self, run_id: int, *, end_reason: str = "run_setup_failed"
    ) -> None:
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
