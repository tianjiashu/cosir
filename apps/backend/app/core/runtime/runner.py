"""Coordinate task lifecycle and workflow execution."""

from app.config.configuration import get_agent_registry, get_tool_system
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.core.delegation.delegation_executor import DelegationExecutor
from app.core.observability import (
    TraceMetadata,
    build_tool_trace_recorder,
    conversation_run_trace,
)
from app.core.observability.tool_trace_recorder import ToolTraceRecorder
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.execution_mode import ExecutionMode
from app.core.tools.schemas import ToolExecutionContext
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.workflows.workflow_operations import WorkflowOperations
from app.hook import HookContext
from app.hook.hook_event import HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.service.depends import (
    get_conversation_run_service,
    get_task_service,
    get_terminal_session_service,
    get_workspace_service,
)
from app.storage.crud.file_snapshot_crud import FileSnapshotCrud


class AgentRuntime:
    """Execute tasks and stream runtime events.

    单一职责：作为执行 / 生命周期引擎，负责任务状态推进、模型流消费、工具调度、
    运行时事件记录、取消与终止保护，以及从 LangGraph checkpoint 派生事件。

    状态单一事实来源是 ``ConversationRun``：本引擎只写 run 执行态；
    取消作用于 run 并中止该 run 的运行循环（模型节点检查取消状态后停止派发工具）。

    职责边界：
    - 负责：任务执行编排、取消，以及把执行异常交给 ConversationRunExecutor 收束。
    - 不负责：checkpoint 回放与历史事件回看（由 Conversation service 提供，细粒度事件仅作
      审计）、工作区 / 任务 /
      轮次的 CRUD 与查询（委托给对应 service 层）；不对外暴露 service 访问器，
      service 仅作为本引擎的私有协作者。
    """

    def __init__(self) -> None:
        """Initialize the execution engine with its private collaborators.

        私有协作者（任务编排、轮次编排、工具调度、agent 目录、工作区解析）全部经
        进程级依赖入口解析，构造时不再逐个传参。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 依赖入口要求的存储或配置尚未初始化时抛出。

        副作用:
            解析并持有进程级 service 单例引用。
        """

        self._task_service = get_task_service()
        self._conversation_run_state_service = get_conversation_run_service()
        self._tool_executor = get_tool_system().executor
        self._agent_registry = get_agent_registry()
        self._workspace_service = get_workspace_service()

    async def execute_run(
        self,
        run: ConversationRunRecord,
        *,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """执行一个已被 ConversationRunExecutor 认领（pending→running）的 run。

        前置条件由执行器保证，本方法不再重复认领或做状态复查：
        1. 调用前执行器已 ``claim_pending_run``，run 处于 running；
        2. 执行器已持有 per-task 锁，同 task 内一次只有一个 running run；
        3. ``run`` 非 None 且为已认领 run。

        本方法只负责三件事：解析 run 绑定的 agent profile、为本次 run 派生独立副本、
        驱动 workflow。所有终态（completed / cancelled / failed）收口与失败日志由
        ConversationRunExecutor 统一负责，本方法不重复处理。

        参数:
            run: 已被执行器认领的 Conversation Run 记录（非 None，状态 running）。

        异常:
            RuntimeError: 轮次绑定的 agent profile 不可用时抛出，由执行器捕获收束为 failed。
        """
        run_id = run.id
        # 解析本次执行的 agent profile：使用 run 创建时绑定的 agent_id
        # （run 维度承载 agent，task 不再绑定 agent），未绑定时回退到 main_agent。
        agent_profile = self._agent_registry.resolve(run.agent_id or "main_agent")
        if agent_profile is None:
            raise RuntimeError(f"agent profile unavailable for run {run_id}")
        # 派生 per-run 副本承载本次 run：共享注册表单例不被原地写，并发 run 互不串扰。
        agent_profile = agent_profile.derive_for_run(run)
        await self.run_agent(agent_profile, execution_mode=execution_mode)

    async def run_agent(
        self,
        agent: AgentProfile,
        *,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """驱动一次 agent run 执行并提交 canonical conversation facts。

        参数:
            agent: 当前 run 的 Agent profile。**必须是 per-run 派生副本**（经
                ``AgentProfile.derive_for_run`` 派生）；禁止传入共享注册表单例，
                run 执行期间可安全写入副本上的运行时字段（如 ``main_agent``），
                不污染共享实例。

        异常:
            RuntimeError: 当 ``agent.run`` 为 None 时抛出。

        副作用:
            触发 USER_PROMPT_SUBMIT/STOP hook、落库并收口对话事实、快照收口、
            断连兜底终态；执行异常仅当 run 仍处于 running 时条件落定 failed
            （``fail_run_if_running``），不覆写已取消/已完成的既有终态；终态已落定
            时异常以降级 warning 留痕（含堆栈），不改变既有终态。
        """

        if agent.run is None:
            raise RuntimeError("agent profile unavailable for run")

        run = agent.run
        run_id = run.id
        task_id = run.task_id
        task = self._task_service.get_task(task_id)
        workspace = self._workspace_service.get_workspace(task.workspace_id)
        # UserPromptSubmit 挂接：本轮已被成功认领后触发。首版 deny 不阻断主流程
        # （run 已认领，硬中断需额外终态收敛，侵入面过大，见 Hook机制技术方案.md §4.2）；
        # 无内置实现，空订阅下 fire 零开销放行。统一经 HookInterceptor 收口。

        await HookInterceptor.async_safe_fire(
            HookContext.from_locatable(event=HookEvent.USER_PROMPT_SUBMIT, turn=run, locatable=None)
        )

        # 自此本连接已持有本轮认领：try/finally 覆盖 RUN_STARTED 之后的全部路径，
        # 确保无论正常完成、异常逃逸还是客户端断开（GeneratorError），终态都只由本连接决定。
        try:
            metadata = TraceMetadata(
                task_id=task_id,
                run_id=run_id,
                agent_id=agent.agent_id,
            )
            recorder = build_tool_trace_recorder()
            operations = self._build_operations(
                workspace, task, run, agent, tool_trace_recorder=recorder
            )
            # 本轮消息轨迹（清空残留、落 user 基线、逐条增量落库）统一由 workflow.run 内
            # 的 RuntimeContextManager 负责（注入 message_store 端口），runner 不再直接落库。

            with conversation_run_trace(metadata) as trace_result:
                await agent.workflow.run(
                    operations,
                    callbacks=trace_result.callbacks,
                    langfuse_trace_id=trace_result.trace_id,
                    execution_mode=execution_mode,
                )
            # Langfuse recorder 使用 SDK 自带的后台批量上报。不能在对话收尾路径
            # 主动调用同步 flush：网络不可用时 SDK 会等待重试，导致 run 无法及时
            # 进入 completed/failed 终态，前端会一直显示运行中。
            # await self._publish_stable_file_changes(run_id)
            # Stop 挂接：本轮正常完成后触发。无内置实现，空订阅下 fire 零开销放行。
            # 统一经 HookInterceptor 收口（异步调度不卡事件循环）。
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=run)
            )
            return
        except Exception as exc:
            log.exception(
                "task_failed",
                extra={
                    "msg": "task execution failed; executor owns terminal settlement",
                    "data": {"task_id": task_id, "run_id": run_id, "error": str(exc)},
                },
            )
            raise
        finally:
            # 终态由 ConversationRunExecutor 条件收口；此处只做资源清理。
            pass

    def _mark_stable_file_changes(self, run_id: int) -> None:
        """把某 run 运行中（``stable=0``）的文件快照收口为已稳定（``stable=1``）。

        抽离为同步方法，以便终态路径（成功/失败/取消/客户端断开）无论是否处于
        async 上下文都能调用：失败与取消分支在同步方法内无法 ``await`` 广播，
        故本方法只做落库标记，广播交由 ``_publish_stable_file_changes``（仅成功路径）。

        参数:
            run_id: 刚结束的轮次标识。

        返回:
            无。

        异常:
            无。快照收口属展示侧增强，失败不应影响 run 主流程，故整体捕获并记 warning。

        副作用:
            把该 run 的 file_snapshots 行置 stable=1。
        """
        try:
            FileSnapshotCrud().mark_stable_by_turn(run_id)
        except Exception:
            log.warning(
                "file_change_mark_stable_failed",
                extra={
                    "msg": "运行中快照收口为稳定失败，不影响 run 结果",
                    "data": {"run_id": run_id},
                },
            )

    async def _publish_stable_file_changes(self, run_id: int) -> None:
        """把本 run 的文件快照收口为稳定事实。"""
        try:
            self._mark_stable_file_changes(run_id)
        except Exception:
            log.warning(
                "file_change_stable_mark_failed",
                extra={
                    "msg": "变更集稳定标记失败，不影响 run 结果",
                    "data": {"run_id": run_id},
                },
            )

    def _resolve_execution_context(
        self,
        task: TaskRecord,
        run_id: int = 0,
    ) -> ToolExecutionContext | None:
        """按 task 解析其所属 workspace 的执行上下文；缺失时返回 None。

        参数:
            task: 当前执行的任务记录；提供 ``workspace_id`` 与 ``task_id``。
            run_id: 当前执行所属轮次标识；用于在工具执行时把文件操作快照关联到
                具体 run，供文件快照回退精准还原。缺省为空字符串。

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
        return ToolExecutionContext.from_workspace(task.id, workspace, run_id=run_id)

    def _build_operations(
        self,
        workspace: WorkspaceRecord,
        task: TaskRecord,
        run: ConversationRunRecord,
        agent_profile: AgentProfile,
        tool_trace_recorder: ToolTraceRecorder | None = None,
    ) -> WorkflowOperations:
        """为单个 run 构建运行时操作门面，按 workspace 解析工具边界。

        workspace 可见性（写、改、删是否开放）由 ``execution_context`` 决定；
        最终「可运行工具集合」由 ``agent_profile.select_tools`` 在候选集上裁定，
        运行底座不再自行做权限门禁。


        参数:
            task: 当前执行的任务记录（已预取，提供 ``workspace_id`` 与 ``task_id``）。
            run: 当前执行的 Conversation Run 记录（提供 ``run_id`` 作为门面绑定）。
            agent_profile: 驱动本轮执行的 agent profile。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。

        返回:
            已注入正确 tool_executor / model_tools / execution_context / trace_recorder 的
            RuntimeOperations 实例。
        """
        model_tools = agent_profile.select_tools(self._tool_executor.list_tools())
        execution_context = self._resolve_execution_context(task, run_id=run.id)
        runtime_dependencies = None
        if execution_context is not None:
            delegate_task_executor = DelegationExecutor(
                child_runner=ChildAgentRunner(
                    self.run_agent,
                    should_cancel=cancellation_registry.is_cancelled,
                ),
                parent_profile=agent_profile,
                parent_run=run,
                parent_task=task,
            )
            runtime_dependencies = ToolRuntimeDependencies(
                delegate_task_executor=delegate_task_executor,
                terminal_session_service=get_terminal_session_service(),
                is_run_cancelled=cancellation_registry.is_cancelled,
            )
        return WorkflowOperations(
            tool_executor=self._tool_executor,
            agent_profile=agent_profile,
            current_run=run,
            current_task=task,
            current_workspace=workspace,
            model_tools=model_tools,
            execution_context=execution_context,
            runtime_dependencies=runtime_dependencies,
            tool_trace_recorder=tool_trace_recorder,
        )
