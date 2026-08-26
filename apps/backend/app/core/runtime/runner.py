"""Coordinate task lifecycle and workflow execution."""

import asyncio
from collections.abc import AsyncGenerator

from app.config.configuration import get_agent_registry, get_tool_system
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.core.delegation.delegation_executor import DelegationExecutor
from app.core.observability import (
    TraceMetadata,
    build_tool_trace_recorder,
    turn_trace,
)
from app.core.runtime.runtime_operations import RuntimeOperations
from app.core.runtime.turn_cancellation_registry import cancellation_registry
from app.hook import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.models import TaskRecord, TurnRecord, WorkspaceRecord
from app.models.delegation_record import DelegationRecord
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload import (
    FileChangeStablePayload,
    RunCancelledPayload,
    RunFailedPayload,
    RunStartedPayload,
)
from app.service.depends import (
    get_delegation_service,
    get_runtime_event_service,
    get_task_service,
    get_turn_service,
    get_workspace_service,
)
from app.service.tool_execution.tool_trace_recorder import ToolTraceRecorder
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud
from app.tools.schemas import ToolExecutionContext
from app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies


class AgentRuntime:
    """Execute tasks and stream runtime events.

    单一职责：作为执行 / 生命周期引擎，负责任务状态推进、模型流消费、工具调度、
    运行时事件记录、取消与终止保护，以及从 LangGraph checkpoint 派生事件。

    状态单一事实来源是 ``Turn``：本引擎只写 turn 执行态，task 执行态由最新 turn 派生；
    取消作用于 turn 并中止该 turn 的运行循环（模型节点检查 turn 取消状态后停止派发工具）。

    职责边界：
    - 负责：任务执行编排、运行时事件（含持久化到 ``runtime_events`` 表以供回放）、取消。
    - 不负责：checkpoint 回放与历史事件回看（历史对话由 ``GET /tasks/{task_id}/turns``
      提供，细粒度事件 timeline 由 ``runtime_events`` 表 + 回放端点提供）、工作区 / 任务 /
      轮次的 CRUD 与查询（委托给对应 service 层）；不对外暴露 service 访问器，
      service 仅作为本引擎的私有协作者。
    """

    def __init__(self) -> None:
        """Initialize the execution engine with its private collaborators.

        参数:
            task_service: 任务编排服务（私有协作者，不对外暴露）。
            turn_service: 轮次编排服务（私有协作者，不对外暴露）。
            context_builder: 文本上下文构建器。
            tool_scheduler: 进程级兜底工具调度器（workspace 缺失时沿用）。
            agent_registry: 进程级 agent profile 目录；引擎按 ``agent_id`` 从中解析
                本次执行由哪个 profile 驱动，自身不再绑定单一 agent。
            workspace_service: 工作区编排服务；提供 ``task → workspace → root_path``
                解析，使破坏性工具以 workspace 根为路径边界。缺省 None 时只暴露
                不要求 workspace context 的只读工具。
            cancellation_registry: 进程内 turn 取消信号注册表；缺省时创建独立实例。
            runtime_event_service: 运行时事件持久化与广播 service。

        返回:
            无。

        异常:
            无。

        副作用:
            持有传入协作者引用；缺省时创建一个进程内取消注册表。
        """

        self._task_service = get_task_service()
        self._turn_service = get_turn_service()
        self._tool_scheduler = get_tool_system().scheduler
        self._agent_registry = get_agent_registry()
        self._workspace_service = get_workspace_service()
        self._runtime_event_service = get_runtime_event_service()

    def cancel_turn(self, turn_id: int) -> TurnRecord:
        """Cancel a turn and mark it cancelled.

        仅允许 ``pending`` / ``running`` 进入 ``cancelled``；已取消轮次幂等返回，已完成 /
        已失败轮次拒绝取消，避免改写历史终态。取消时同时写入进程内取消信号和
        ``RUN_CANCELLED`` 持久化事件。

        参数:
            turn_id: 待取消的轮次标识。

        返回:
            取消后的 ``TurnRecord``。

        异常:
            ValueError: 当 turn 已处于 completed/failed 等不可取消终态时抛出。

        副作用:
            更新 turn 状态、写入进程内取消信号、持久化 run_cancelled 事件并记录日志。
        """

        before_cancel = self._turn_service.get_turn(turn_id)
        if before_cancel.status == "cancelled":
            cancellation_registry.mark_cancelled(turn_id)
            return before_cancel
        if before_cancel.status not in {"pending", "running"}:
            raise ValueError(f"cannot cancel turn in status {before_cancel.status}")

        cancellation_registry.mark_cancelled(turn_id)
        turn = self._turn_service.cancel_turn_if_active(turn_id, "user_cancelled")
        if turn is None:
            after_race = self._turn_service.get_turn(turn_id)
            if after_race.status == "cancelled":
                return after_race
            raise ValueError(f"cannot cancel turn in status {after_race.status}")
        try:
            self._cancel_active_child_turns(turn)
            # 取消终态事件不由本方法广播：实际检测到取消的执行节点
            # （model_node 流式中断 / tools_node 执行前检查）才发出携带 token 的
            # RUN_CANCELLED，避免与执行节点的终态事件重复投影成两张「任务已取消」。
        except RuntimeError:
            log.exception(
                "turn_cancelled_event_persist_failed",
                extra={
                    "msg": "turn 已取消，但取消事件持久化失败",
                    "data": {"turn_id": turn_id, "task_id": turn.task_id},
                },
            )
        log.info(
            "turn_cancelled",
            extra={
                "msg": "turn cancelled",
                "data": {"turn_id": turn_id, "task_id": turn.task_id},
            },
        )
        return turn

    def _cancel_active_child_turns(self, parent_turn: TurnRecord) -> None:
        """取消 parent turn 下仍处于活动状态的 child delegation。

        参数:
            parent_turn: 已被取消的 parent turn 记录。

        返回:
            无。

        异常:
            无；级联取消失败会记录日志并继续父 turn 取消流程。

        副作用:
            读取 delegation 记录，标记 child turn 取消信号，尽力更新 child turn 与 delegation 终态。
        """

        try:
            active_delegations = get_delegation_service().list_active_by_parent_turn(
                parent_turn.id
            )
        except Exception:
            log.exception(
                "delegation_child_cancel_scan_failed",
                extra={
                    "msg": "父 turn 已取消，但扫描活动 child delegation 失败",
                    "data": {
                        "parent_turn_id": parent_turn.id,
                        "task_id": parent_turn.task_id,
                    },
                },
            )
            return
        for delegation in active_delegations:
            self._cancel_child_delegation(parent_turn, delegation)

    def _cancel_child_delegation(
        self,
        parent_turn: TurnRecord,
        delegation: DelegationRecord,
    ) -> None:
        """取消单个 child delegation 及其 child turn。

        参数:
            parent_turn: 已被取消的 parent turn 记录。
            delegation: 需要级联取消的 delegation 记录。

        返回:
            无。

        异常:
            无；单个 child 取消失败会记录日志并继续处理其他 child。

        副作用:
            可能更新 child turn 状态、写入 child RUN_CANCELLED 事件、更新 delegation 状态。
        """

        reason = "parent_turn_cancelled"
        try:
            if delegation.child_turn_id:
                cancellation_registry.mark_cancelled(delegation.child_turn_id)
                child_turn = self._turn_service.cancel_turn_if_active(
                    delegation.child_turn_id,
                    reason,
                )
                if child_turn is not None:
                    self._save_and_publish_runtime_event(
                        RuntimeEvent(
                            event_type=EventType.RUN_CANCELLED,
                            task_id=child_turn.task_id,
                            payload=RunCancelledPayload(status="cancelled"),
                            turn_id=child_turn.id,
                        )
                    )
            get_delegation_service().mark_cancelled(delegation.id, reason)
        except Exception:
            log.exception(
                "delegation_child_cancel_failed",
                extra={
                    "msg": "父 turn 已取消，但级联取消 child delegation 失败",
                    "data": {
                        "parent_turn_id": parent_turn.id,
                        "delegation_id": delegation.id,
                        "child_turn_id": delegation.child_turn_id,
                    },
                },
            )

    async def run_turn(
        self,
        turn: TurnRecord | None = None,
    ) -> AsyncGenerator[RuntimeEvent] | None:
        """执行单个 pending 轮次并实时流式产出运行时事件。

        只负责「pending → 认领 → 执行 → 流式事件」。逐条事件在 ``yield`` 前经 ``emit``
        持久化到 ``runtime_events`` 表（带本轮自增 sequence），供刷新 / 重连后通过回放端点
        重建细粒度 timeline；历史对话列表由 ``GET /tasks/{task_id}/turns`` 提供。SSE 通过
        断开兜底（finally 标记 failed）判定本轮是否中断。非 pending 轮次不应进入本方法，
        调用方（API 层）应先做 409 守卫；此处仅做防御性早退。

        参数:
            turn_id: 需要运行的轮次标识符。
            turn: 可选的预取轮次记录；缺省时按 ``turn_id`` 读取。
        """
        if turn is None:
            log.warning(
                "run_turn_missing_turn",
                extra={
                    "msg": "run_turn called without a turn; refusing to execute",
                    "data": {},
                },
            )
            return None

        turn_id = turn.id

        # 非 pending 轮次不应进入本方法，调用方（API 层）应先做 409 守卫；此处仅做防御性早退。
        if turn.status != "pending":
            log.warning(
                "run_turn_non_pending",
                extra={
                    "msg": "run_turn called for non-pending turn; refusing to execute",
                    "data": {"turn_id": turn_id, "status": turn.status},
                },
            )
            return None

        # 解析本次执行的 agent profile：优先使用轮次创建时绑定的 agent_id，
        # 未绑定时回退到 task.agent_id 默认归属。
        agent_profile: AgentProfile | None = self._agent_registry.resolve(
            turn.agent_id or "main_agent"
        )
        if agent_profile is None:
            raise RuntimeError(f"agent profile unavailable for turn {turn_id}")

        if not self._turn_service.claim_pending_turn(turn.id):
            # 已被其它连接抢占（极小概率的竞态）：本轮不再重复驱动，直接退出。
            # 关键：未成功认领即在进入下方 try/finally 之前 return，断开兜底只由真正
            # 持有本轮的连接负责，避免落败连接误标他连接正在驱动的 running turn。
            log.warning(
                "turn_claim_lost",
                extra={
                    "msg": "turn already claimed by another connection",
                    "data": {"turn_id": turn_id},
                },
            )
            return None

        # 派生 per-run 副本承载本轮 turn：共享注册表单例不被原地写，并发 turn 互不串扰。
        agent_profile = agent_profile.derive_for_turn(turn)
        return self.run_agent(agent_profile)

    async def run_agent(self, agent: AgentProfile) -> AsyncGenerator[RuntimeEvent, None]:
        """驱动一次 agent turn 执行并逐条透传运行时事件。

        参数:
            agent: 当前 turn 的 Agent profile。**必须是 per-run 派生副本**（经
                ``AgentProfile.derive_for_turn`` 派生）；禁止传入共享注册表单例，
                turn 执行期间可安全写入副本上的运行时字段（如 ``main_agent``），
                不污染共享实例。

        返回:
            逐条产生的 ``RuntimeEvent`` 异步生成器。

        异常:
            RuntimeError: 当 ``agent.turn`` 为 None 时抛出。

        副作用:
            触发 USER_PROMPT_SUBMIT/STOP hook、落库并广播运行时事件、快照收口、
            断连兜底终态；执行异常仅当 turn 仍处于 running 时条件落定 failed
            （``fail_turn_if_running``），不覆写已取消/已完成的既有终态；终态已落定
            时异常以降级 warning 留痕（含堆栈），不改变既有终态。
        """

        if agent.turn is None:
            raise RuntimeError("agent profile unavailable for turn")

        turn = agent.turn
        turn_id = turn.id
        task_id = turn.task_id
        task = self._task_service.get_task(task_id)
        workspace = self._workspace_service.get_workspace(task.workspace_id)
        # UserPromptSubmit 挂接：本轮已被成功认领后触发。首版 deny 不阻断主流程
        # （turn 已认领，硬中断需额外终态收敛，侵入面过大，见 Hook机制技术方案.md §4.2）；
        # 无内置实现，空订阅下 fire 零开销放行。统一经 HookInterceptor 收口。

        await HookInterceptor.async_safe_fire(
            HookContext.from_locatable(
                event=HookEvent.USER_PROMPT_SUBMIT, turn=turn, locatable=None
            )
        )

        # 自此本连接已持有本轮认领：try/finally 覆盖 RUN_STARTED 之后的全部路径，
        # 确保无论正常完成、异常逃逸还是客户端断开（GeneratorError），终态都只由本连接决定。
        try:
            yield await self._emit(
                RuntimeEvent(
                    event_type=EventType.RUN_STARTED,
                    task_id=task_id,
                    payload=RunStartedPayload(status="running", agent_id=agent.agent_id),
                    turn_id=turn_id,
                    is_main_agent=agent.main_agent,
                ),
                agent,
            )

            metadata = TraceMetadata(
                task_id=task_id,
                turn_id=turn_id,
                agent_id=agent.agent_id,
            )
            recorder = build_tool_trace_recorder()
            operations = self._build_operations(
                workspace, task, turn, agent, tool_trace_recorder=recorder
            )
            # 本轮消息轨迹（清空残留、落 user 基线、逐条增量落库）统一由 workflow.run 内
            # 的 RuntimeContextManager 负责（注入 message_store 端口），runner 不再直接落库。

            with turn_trace(metadata) as trace_result:
                async for event in agent.workflow.run(
                    operations,
                    callbacks=trace_result.callbacks,
                    langfuse_trace_id=trace_result.trace_id,
                ):
                    yield await self._emit(event, agent)
            try:
                recorder.flush()
            except Exception:
                log.exception(
                    "langfuse_recorder_flush_unhandled",
                    extra={
                        "msg": "工具 trace recorder flush 未处理异常，已忽略以避免影响 turn",
                        "data": {"task_id": task_id, "turn_id": turn_id},
                    },
                )
            await self._publish_stable_file_changes(task_id, turn_id, agent.main_agent)
            # Stop 挂接：本轮正常完成后触发。无内置实现，空订阅下 fire 零开销放行。
            # 统一经 HookInterceptor 收口（异步调度不卡事件循环）。
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=turn)
            )
            return
        except Exception as exc:
            # 真执行异常：仅当本轮仍处于 running 时才条件落定 failed（fail_turn_if_running），
            # 避免用户取消（cancelled）等已落终态被异常冒泡覆写；end_reason 保持 None
            # （语义化枚举码，不把自由文本异常塞进枚举字段）。客户端断开由 finally 的
            # _mark_turn_disconnected_if_running 单独落 end_reason="client_disconnected"
            # （turn 表），与本 payload 不冲突。
            failed_turn = self._turn_service.fail_turn_if_running(turn_id, end_reason=None)
            if failed_turn is None:
                # 终态已被取消/完成等先行落定：不覆写历史终态，也跳过 RUN_FAILED 投影，
                # 避免与已有终态事件（如 RUN_CANCELLED）重复矛盾；真实异常仍须留痕
                # （warning 级，终态非 failed，不打 task_failed 语义）。
                log.warning(
                    "task_failed_after_terminal",
                    extra={
                        "msg": "turn terminal already settled; exception not overriding",
                        "data": {
                            "task_id": task_id,
                            "turn_id": turn_id,
                            "exception": str(exc),
                        },
                    },
                    exc_info=True,
                )
                return
            # 失败即终态：把本 turn 运行中（stable=0）的快照收口为稳定，
            # 使运行后变更能展示与撤销。同步调用（此处非 await 上下文）。
            self._mark_stable_file_changes(turn_id)

            log.exception(
                "task_failed",
                extra={
                    "msg": "task execution failed",
                    "data": {
                        "task_id": task_id,
                    },
                },
            )
            yield await self._emit(
                RuntimeEvent(
                    event_type=EventType.RUN_FAILED,
                    task_id=task_id,
                    turn_id=turn_id,
                    payload=RunFailedPayload(
                        status="failed",
                        error=str(exc),
                        end_reason=None,
                    ),
                    is_main_agent=agent.main_agent,
                ),
                agent,
            )
        finally:
            # 本连接持有的清理收口：仅当本轮仍卡在 running（客户端断开导致运行被中止、
            # 或落终态前异常逃逸）时置 failed；正常完成 / 已失败 / 已取消均为幂等空操作。
            self._mark_turn_disconnected_if_running(turn_id)

    def _mark_turn_disconnected_if_running(self, turn_id: int) -> None:
        """本连接持有的轮次若仍处于 ``running``，则落定为断开失败。

        仅在本引擎成功认领（claim）本轮后进入的清理路径（``run_turn`` 的 finally）中调用：
        正常完成 / 失败 / 取消时 turn 已落终态（非 ``running``），此处为幂等空操作；仅当
        客户端断开导致运行被中止、或落终态前异常逃逸使 turn 卡在 ``running`` 时，才置为
        ``failed``（``end_reason="client_disconnected"``），避免孤儿 ``running``。由于只有
        持有认领的连接才会走到这里，不会误标他连接正在驱动的 ``running`` turn。

        参数:
            turn_id: 待检查并可能落终态的轮次标识。

        返回:
            无。

        异常:
            不向上抛出：轮次可能已被清理，内部窄异常保护，避免 teardown 抛异常掩盖主流程结果。

        副作用:
            可能把 turn 状态由 ``running`` 置为 ``failed`` 并写日志。
        """

        try:
            failed_turn = self._turn_service.fail_turn_if_running(
                turn_id, end_reason="client_disconnected"
            )
            if failed_turn is None:
                return
            log.info(
                "turn_marked_disconnected",
                extra={
                    "msg": f"客户端断开，轮次已标记为 failed，turn_id={turn_id}",
                    "data": {"turn_id": turn_id, "end_reason": "client_disconnected"},
                },
            )
            # 客户端断开致 failed，同样收口快照为稳定，保证变更可见与可撤销。
            self._mark_stable_file_changes(turn_id)
        except Exception:
            log.exception(
                "turn_disconnect_mark_failed",
                extra={
                    "msg": f"客户端断开后标记轮次 failed 失败，turn_id={turn_id}",
                    "data": {"turn_id": turn_id},
                },
            )

    def _publish_runtime_event(
        self,
        event: RuntimeEvent,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Publish a runtime event through the configured event service.

        参数:
            event: 待发布的运行时事件。
            runtime_event_loop: 可选的所属事件循环。为 None 时在当前线程直接广播；
                非 None 时经 ``call_soon_threadsafe`` 调度到该 loop 线程广播（用于
                非 loop 线程落库后的跨线程发布）。缺省为 None。

        返回:
            无。

        异常:
            RuntimeError: 当 ``runtime_event_loop`` 已关闭时，``call_soon_threadsafe``
                会抛出；发布失败由调用方决定是否兜底。

        副作用:
            向 RuntimeEventBus 订阅队列发布事件。
        """

        if runtime_event_loop is None:
            self._runtime_event_service.publish_event(event)
            return
        runtime_event_loop.call_soon_threadsafe(
            self._runtime_event_service.publish_event,
            event,
        )

    def _save_and_publish_runtime_event(
        self,
        event: RuntimeEvent,
        runtime_event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> RuntimeEvent:
        """Persist and publish a runtime event.

        参数:
            event: 待保存并发布的运行时事件。
            runtime_event_loop: 可选的所属事件循环，透传给 ``_publish_runtime_event``
                决定广播方式（None 直发 / 非 None 跨线程调度）。缺省为 None。

        返回:
            已赋真实 sequence 的 RuntimeEvent。

        异常:
            RuntimeError: 当 runtime event 持久化失败，或广播阶段 ``runtime_event_loop``
                已关闭时抛出。

        副作用:
            向 runtime_events 表写入事件，并广播给当前订阅者。
        """

        stamped = self._runtime_event_service.save_event(event)
        self._publish_runtime_event(stamped, runtime_event_loop)
        return stamped

    def _mark_stable_file_changes(self, turn_id: int) -> None:
        """把某 turn 运行中（``stable=0``）的文件快照收口为已稳定（``stable=1``）。

        抽离为同步方法，以便终态路径（成功/失败/取消/客户端断开）无论是否处于
        async 上下文都能调用：失败与取消分支在同步方法内无法 ``await`` 广播，
        故本方法只做落库标记，广播交由 ``_publish_stable_file_changes``（仅成功路径）。

        参数:
            turn_id: 刚结束的轮次标识。

        返回:
            无。

        异常:
            无。快照收口属展示侧增强，失败不应影响 turn 主流程，故整体捕获并记 warning。

        副作用:
            把该 turn 的 file_snapshots 行置 stable=1。
        """
        try:
            FileSnapshotCrud().mark_stable_by_turn(turn_id)
        except Exception:
            log.warning(
                "file_change_mark_stable_failed",
                extra={
                    "msg": "运行中快照收口为稳定失败，不影响 turn 结果",
                    "data": {"turn_id": turn_id},
                },
            )

    async def _publish_stable_file_changes(
        self, task_id: int, turn_id: int, is_main_agent: bool
    ) -> None:
        """把本 turn 的文件快照标记为已稳定，并逐条广播 file_change_stable 事件。

        turn 结束意味着其内所有工具调用已定稿，此时变更才对用户可见、可撤销。
        事件仅广播（不持久化到 ``runtime_events`` 表）：它只是展示侧增量通知，
        数据源在 ``file_snapshots`` 表，前端可靠 ``GET /tasks/{id}/changes`` 全量校准。

        参数:
            task_id: 所属任务标识。
            turn_id: 刚结束的轮次标识。
            is_main_agent: 是否主 Agent 轮次，透传到每条 file_change_stable 事件供
                前端区分主/子 Agent 变更。

        返回:
            无。

        异常:
            无。变更集广播属展示侧增强，任何失败都不应让已成功的 turn 被判为失败，
            故整体捕获并记 warning；前端仍可靠全量查询校准。

        副作用:
            把该 turn 的 file_snapshots 行置 stable=1；向事件总线广播若干事件。
        """
        try:
            self._mark_stable_file_changes(turn_id)
            crud = FileSnapshotCrud()
            snapshots = crud.list_stable_by_task(task_id, [turn_id])
            if not snapshots:
                return
            for snapshot in snapshots:
                self._publish_runtime_event(
                    RuntimeEvent(
                        event_type=EventType.FILE_CHANGE_STABLE,
                        task_id=task_id,
                        payload=FileChangeStablePayload(
                            task_id=task_id,
                            turn_id=turn_id,
                            path=snapshot.path,
                            action=snapshot.action,
                        ),
                        is_main_agent=is_main_agent,
                        turn_id=turn_id,
                    )
                )
        except Exception:
            log.warning(
                "file_change_stable_publish_failed",
                extra={
                    "msg": "变更集稳定标记或事件广播失败，不影响 turn 结果",
                    "data": {"task_id": task_id, "turn_id": turn_id},
                },
            )

    def _resolve_execution_context(
        self, task: TaskRecord, turn_id: int = 0
    ) -> ToolExecutionContext | None:
        """按 task 解析其所属 workspace 的执行上下文；缺失时返回 None。

        参数:
            task: 当前执行的任务记录；提供 ``workspace_id`` 与 ``task_id``。
            turn_id: 当前执行所属轮次标识；用于在工具执行时把文件操作快照关联到
                具体 turn，供 Turn 回退精准还原。缺省为空字符串。

        返回:
            命中 workspace 时返回 ToolExecutionContext；workspace 缺失或
            workspace_service 未注入时返回 None。

        异常:
            仅当 workspace 不存在（``KeyError``）时返回 None 并记 warning；
            数据库层异常（如 SQLAlchemyError）按原样冒泡，由上层 ``run_agent`` 记为
            task_failed，不做静默降级。

        副作用:
            workspace 不存在时记 warning 日志。
        """

        try:
            workspace = self._workspace_service.get_workspace(task.workspace_id)
        except KeyError:
            log.warning(
                "workspace_not_found_for_task",
                extra={
                    "msg": "任务所属 workspace 不存在，破坏性工具将不可用",
                    "data": {"task_id": task.id, "workspace_id": task.workspace_id},
                },
            )
            return None
        return ToolExecutionContext.from_workspace(task.id, workspace, turn_id=turn_id)

    async def _emit(
        self,
        event: RuntimeEvent,
        agent_profile: AgentProfile,
    ) -> RuntimeEvent:
        """落库并透传一条运行时事件（赋唯一递增 sequence）。

        在 ``yield`` 前调用：经 ``RuntimeEventService`` 以独立线程写入 ``runtime_events`` 表
        （避免阻塞 SSE 事件循环），并使用存储层分配的真实 turn-local sequence。
        落库抛 ``RuntimeError`` 时由本方法记 error 日志并降级透传原事件，不中断流式运行。

        参数:
            event: 待落库并透传的运行时事件。
            agent_profile: 当前 agent profile，其 ``runtime_event_loop`` 非空时经
                ``call_soon_threadsafe`` 跨线程广播。

        返回:
            已赋序号、可直接 ``yield`` 的事件；落库失败时为原事件。

        异常:
            落库抛 ``RuntimeError`` 被本方法吞掉（记日志降级透传）；落库抛其他异常或
            广播阶段 ``runtime_event_loop`` 已关闭时，异常向上冒泡。

        副作用:
            向 ``runtime_events`` 表插入一行（失败仅记日志）；向事件总线广播。
        """

        try:
            stamped = await asyncio.to_thread(self._runtime_event_service.save_event, event)
        except RuntimeError:
            log.exception(
                "runtime_event_emit_persist_failed",
                extra={
                    "msg": "运行时事件持久化失败，继续透传实时事件",
                    "data": {
                        "event_id": event.event_id,
                        "event_type": event.event_type.value,
                        "turn_id": event.turn_id,
                    },
                },
            )
            stamped = event
        self._publish_runtime_event(stamped, agent_profile.runtime_event_loop)
        return stamped

    def _build_operations(
        self,
        workspace: WorkspaceRecord,
        task: TaskRecord,
        turn: TurnRecord,
        agent_profile: AgentProfile,
        tool_trace_recorder: ToolTraceRecorder | None = None,
    ) -> RuntimeOperations:
        """为单个 turn 构建运行时操作门面，按 workspace 解析工具边界。

        workspace 可见性（写、改、删是否开放）由 ``execution_context`` 决定；
        最终「可运行工具集合」由 ``agent_profile.select_tools`` 在候选集上裁定，
        运行底座不再自行做权限门禁。


        参数:
            task: 当前执行的任务记录（已预取，提供 ``workspace_id`` 与 ``task_id``）。
            turn: 当前执行的轮次记录（提供 ``turn_id`` 作为门面绑定）。
            agent_profile: 驱动本轮执行的 agent profile。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。

        返回:
            已注入正确 tool_scheduler / model_tools / execution_context / trace_recorder 的
            RuntimeOperations 实例。
        """
        model_tools = agent_profile.select_tools(self._tool_scheduler.list_tools())
        execution_context = self._resolve_execution_context(task, turn_id=turn.id)
        runtime_dependencies = None
        if execution_context is not None:
            delegate_task_executor = DelegationExecutor(
                child_runner=ChildAgentRunner(
                    self.run_agent,
                    should_cancel=cancellation_registry.is_cancelled,
                ),
                parent_profile=agent_profile,
                parent_turn=turn,
                parent_task=task,
            )
            runtime_dependencies = ToolRuntimeDependencies(
                delegate_task_executor=delegate_task_executor
            )
        return RuntimeOperations(
            tool_scheduler=self._tool_scheduler,
            agent_profile=agent_profile,
            current_turn=turn,
            current_task=task,
            current_workspace=workspace,
            model_tools=model_tools,
            execution_context=execution_context,
            runtime_dependencies=runtime_dependencies,
            tool_trace_recorder=tool_trace_recorder,
        )
