"""Coordinate task lifecycle and workflow execution."""

import asyncio
from dataclasses import replace

from langchain_core.messages import SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.config.configuration import get_agent_registry, get_tool_system
from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileConfigError,
    AgentProfileType,
)
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.model_settings import ModelSettings
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.observability import (
    TraceMetadata,
    conversation_run_trace,
)
from app.core.observability.tool_trace_recorder import ToolTraceRecorder
from app.core.runtime.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.runtime.execution_mode import ExecutionMode
from app.core.runtime.tool_call_cancellation_registry import (
    tool_call_cancellation_registry,
)
from app.core.tools.schemas import ToolDefinition, ToolExecutionContext
from app.core.tools.schemas.tool_names import TOOL_PROPOSE_AGENT_CONFIGURATION
from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.workflows.workflow_operations import WorkflowOperations
from app.core.workflows.agent_workflow import WorkflowOutcome
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.service.depends import (
    get_conversation_run_observability_service,
    get_model_config_service,
    get_task_service,
    get_terminal_session_service,
    get_workspace_service,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class AgentRuntime:
    """Execute tasks and stream runtime events.

    单一职责：作为执行 / 生命周期引擎，负责任务状态推进、模型流消费、工具调度、
    运行时事件记录、取消与终止保护，以及从 LangGraph checkpoint 派生事件。

    状态单一事实来源是 ``ConversationRun``：本引擎只写 run 执行态；
    取消作用于 run 并中止该 run 的运行循环（模型节点检查取消状态后停止派发工具）。

    职责边界：
    - 负责：任务执行编排、取消，以及把执行异常向上传播（run 终态由 workflow 落定）。
    - 不负责：checkpoint 回放与历史事件回看（由 Conversation service 提供，细粒度事件仅作
      审计）、工作区 / 任务 /
      轮次的 CRUD 与查询（委托给对应 service 层）；不对外暴露 service 访问器，
      service 仅作为本引擎的私有协作者。
    """

    def __init__(
        self,
        *,
        process_tool_output_channel_factory: (ProcessToolOutputChannelFactory | None) = None,
    ) -> None:
        """Initialize the execution engine with its private collaborators.

        私有协作者（任务编排、轮次编排、工具调度、agent 目录、工作区解析）全部经
        进程级依赖入口解析；进程工具输出通道工厂由应用装配入口注入，避免核心 runtime
        依赖 Assistant Transport。

        参数:
            process_tool_output_channel_factory: 可选的进程工具输出通道工厂，由应用装配层
                注入；缺省时不投递进程工具的运行期输出事件。

        返回:
            无。

        异常:
            RuntimeError: 依赖入口要求的存储或配置尚未初始化时抛出。

        副作用:
            解析并持有进程级 service 单例引用；工具执行器同样在构造期从进程级工具系统解析，
            配置变更后需要重装配时由 :meth:`reload_tool_executor` 显式刷新。
        """

        self._task_service = get_task_service()
        self._tool_executor = get_tool_system().executor
        self._tool_register = get_tool_system().registry
        self._agent_registry = get_agent_registry()
        self._workspace_service = get_workspace_service()
        self._process_tool_output_channel_factory = process_tool_output_channel_factory

    async def execute_run(
        self,
        run: ConversationRunRecord,
        *,
        execution_mode: ExecutionMode = "fresh",
    ) -> WorkflowOutcome:
        """执行一个已被 ConversationRunExecutor 认领（pending→running）的 run。

        前置条件由执行器保证，本方法不再重复认领或做状态复查：
        1. 调用前执行器已 ``claim_pending_run``，run 处于 running；
        2. 同 task 内一次只有一个 running run：由准入（``prepare_run_start`` 拒绝已有
           active run）与 ``claim_pending_run`` 的原子条件更新共同保证，不依赖进程内锁；
        3. ``run`` 非 None 且为已认领 run。

        本方法只负责三件事：解析 run 绑定的 agent profile、为本次 run 派生独立副本、
        驱动 workflow。所有终态（completed / cancelled / failed）由 **workflow** 落定
        （节点内经 ``WorkflowOperations`` 调 run state service），本方法不落任何终态。

        参数:
            run: 已被执行器认领的 Conversation Run 记录（非 None，状态 running）。
            execution_mode: 本次执行是 ``fresh`` 还是从既有 checkpoint 恢复（``resume``）；
                透传给 workflow，由其决定是否清空旧上下文与如何构造 graph 输入。

        返回:
            ``finished`` 表示 workflow 已收束；``waiting_for_input`` 表示需保留 checkpoint，
            由执行器完成本地收尾后迁移 Run 状态。

        异常:
            RuntimeError: 轮次绑定的 agent profile 不可用时抛出，由执行器捕获收束为 failed。
        """
        run_id = run.id
        task = self._task_service.get_task(run.task_id)
        workspace = self._workspace_service.get_workspace(task.workspace_id)
        agent_id = run.agent_id or "main_agent"
        is_main_agent = agent_id == "main_agent"
        workspace_scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE if is_main_agent else workspace.root_path
        )
        profile_snapshot = (
            task.extra.get("agent_team_profile_snapshot")
            if isinstance(task.extra, dict)
            else None
        )
        try:
            agent_profile = self._agent_registry.resolve(workspace_scope, agent_id)
        except AgentProfileConfigError as exc:
            if not isinstance(profile_snapshot, dict):
                raise RuntimeError(
                    f"workspace child agent configuration unavailable for run {run_id}: {exc}"
                ) from exc
            agent_profile = None
        if agent_profile is None and not isinstance(profile_snapshot, dict):
            raise RuntimeError(f"agent profile unavailable for run {run_id}")
        if isinstance(profile_snapshot, dict):
            snapshot_settings = profile_snapshot.get("model_settings", {})
            if not isinstance(snapshot_settings, dict):
                raise RuntimeError(f"agent team profile snapshot is invalid for run {run_id}")
            snapshot_agent_id = str(
                profile_snapshot.get("agent_id", agent_id)
            )
            snapshot_role = str(profile_snapshot.get("role", snapshot_agent_id))
            snapshot_prompt = str(profile_snapshot.get("system_prompt", ""))
            snapshot_tools = list(profile_snapshot.get("allowed_tools", []))
            snapshot_max_steps = int(profile_snapshot.get("max_steps", 100))
            snapshot_structured_output = profile_snapshot.get("structured_output")
            structured_output = (
                StructuredOutputSpec.model_validate(snapshot_structured_output)
                if snapshot_structured_output is not None
                else None
            )
            if agent_profile is None:
                agent_profile = AgentProfile(
                    agent_id=snapshot_agent_id,
                    role=snapshot_role,
                    system_prompt=snapshot_prompt,
                    allowed_tools=snapshot_tools,
                    agent_type=AgentProfileType.CHILD,
                    max_steps=snapshot_max_steps,
                    structured_output=structured_output,
                )
            agent_profile = replace(
                agent_profile,
                agent_id=snapshot_agent_id,
                role=snapshot_role,
                system_prompt=str(
                    profile_snapshot.get("system_prompt", agent_profile.system_prompt)
                ),
                allowed_tools=snapshot_tools or agent_profile.allowed_tools,
                max_steps=snapshot_max_steps,
                structured_output=structured_output,
                model_config_id=profile_snapshot.get(
                    "model_config_id", agent_profile.model_config_id
                ),
                model_settings=ModelSettings(**snapshot_settings),
            )
        # Run 选择的模型配置在 per-run 派生边界物化；进入 workflow 后只允许消费
        # AgentProfile.model_settings，不再让模型工厂回查 model_config_id。
        runtime_model_settings = None
        if isinstance(profile_snapshot, dict):
            # Agent Team 节点使用确认时冻结的完整模型设置；不能因用户随后编辑或删除
            # model_config 而改变已经确认的 Team。
            snapshot_settings = profile_snapshot.get("model_settings")
            if not isinstance(snapshot_settings, dict):
                raise RuntimeError(f"agent team model snapshot is invalid for run {run_id}")
            runtime_model_settings = ModelSettings(**snapshot_settings)
        elif run.model_config_id is not None:
            runtime_model_settings = ModelSettings.from_model_config_record(
                get_model_config_service().get_config(run.model_config_id)
            )

        # 派生 per-run 副本承载本次 run：共享注册表单例不被原地写，并发 run 互不串扰。
        agent_profile = agent_profile.derive_for_run(
            run,
            model_settings=runtime_model_settings,
        )
        return await self.run_agent(
            agent_profile,
            execution_mode=execution_mode,
        )

    async def run_agent(
        self, agent: AgentProfile, *, execution_mode: ExecutionMode = "fresh"
    ) -> WorkflowOutcome:
        """驱动一次 agent run 执行并提交 canonical conversation facts。

        参数:
            agent: 当前 run 的 Agent profile。**必须是 per-run 派生副本**（经
                ``AgentProfile.derive_for_run`` 派生）；禁止传入共享注册表单例，
                run 执行期间可安全写入副本上的运行时字段（如 ``main_agent``），
                不污染共享实例。
            execution_mode: 本次执行是 ``fresh`` 还是 ``resume``，原样透传给
                ``agent.workflow.run``。

        异常:
            RuntimeError: 当 ``agent.run`` 为 None 时抛出。

        副作用:
            触发 USER_PROMPT_SUBMIT/STOP hook；run 的终态（completed / cancelled / failed）
            **由 workflow 落定**——正常路径经节点内的 ``WorkflowOperations``，异常路径经
            ``ReactLikeWorkflow._settle_failed_run``；本方法不写任何终态，异常按原文传播并
            记 ``task_failed``，仅清理进程内取消信号。本轮消息落库、canonical conversation
            facts 与快照收口由 ``workflow.run`` 内部的 ``RuntimeContextManager`` 负责。
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
            async with conversation_run_trace(metadata) as trace_result:
                if trace_result.trace_id is not None:
                    try:
                        get_conversation_run_observability_service().set_langfuse_trace_id(
                            run_id,
                            trace_result.trace_id,
                        )
                    except Exception:
                        log.exception(
                            "langfuse_trace_id_persist_failed",
                            extra={
                                "msg": "Langfuse trace_id 持久化失败，继续执行 Agent",
                                "data": {"run_id": run_id, "task_id": task_id},
                            },
                        )
                operations = self._build_operations(
                    workspace,
                    task,
                    run,
                    agent,
                    tool_trace_recorder=trace_result.tool_trace_recorder,
                )
                # 本轮消息轨迹（清空残留、落 user 基线、逐条增量落库）统一由 workflow.run 内
                # 的 RuntimeContextManager 负责（注入 message_store 端口），runner 不再直接落库。
                outcome = await agent.workflow.run(
                    operations,
                    callbacks=trace_result.callbacks,
                    langfuse_trace_id=trace_result.trace_id,
                    execution_mode=execution_mode,
                )
            # Langfuse recorder 使用 SDK 自带的后台批量上报。不能在对话收尾路径
            # 主动调用同步 flush：网络不可用时 SDK 会等待重试，导致 run 无法及时
            # 进入 completed/failed 终态，前端会一直显示运行中。
            # Stop 挂接：本轮正常完成后触发。无内置实现，空订阅下 fire 零开销放行。
            # 统一经 HookInterceptor 收口（异步调度不卡事件循环）。
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=run)
            )
            return outcome
        except Exception as exc:
            log.exception(
                "task_failed",
                extra={
                    "msg": "run 执行失败，异常向上传播（终态已由 workflow 在抛出前落定）",
                    "data": {"task_id": task_id, "run_id": run_id, "error": str(exc)},
                },
            )
            raise
        finally:
            if isinstance(task.extra, dict) and task.extra.get("agent_team_run_id"):
                from app.agent_team.coordinator import get_agent_team_coordinator

                await asyncio.to_thread(
                    get_agent_team_coordinator().handle_node_natural_completion,
                    run_id,
                )
            cancellation_registry.clear(run_id)
            # 工具级信号由工具执行层在单次调用结束时释放；这里兜底回收「点名了已结束的
            # 工具调用」这类不会再被消费的信号，避免进程内信号随会话累积。
            tool_call_cancellation_registry.clear_run(run_id)

    def _resolve_execution_context(
        self,
        task: TaskRecord,
        run_id: int = 0,
    ) -> ToolExecutionContext | None:
        """按 task 解析其所属 workspace 的执行上下文；缺失时返回 None。

        参数:
            task: 当前执行的任务记录；提供 ``workspace_id`` 与 ``task_id``。
            run_id: 当前执行所属轮次标识，供工具执行上下文和取消边界使用；缺省为 0。

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

        Task 固化的模型 schema 决定 ``bind_tools`` 的稳定结构；当前 Run 的
        Run Extra 持久化的 ``ban_tools`` 与 Task 固化 schema 在本方法内计算出的
        ``allows_tools`` 决定工具调用和执行准入。workspace 可见性仍由
        ``execution_context`` 负责，不能替代两者。

        参数:
            workspace: 当前 run 所属的 workspace 记录，用于解析工具执行边界。
            task: 当前执行的任务记录（已预取，提供 ``workspace_id`` 与 ``task_id``）。
            run: 当前执行的 Conversation Run 记录（提供 ``run_id`` 作为门面绑定）。
            agent_profile: 驱动本轮执行的 agent profile。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。
            agent_profile_registry: 进程内 Agent 目录，作为本 run 的运行期依赖透传给工具
                （委派执行期按 workspace 作用域解析目标）；为 None 时主 Agent 不暴露
                ``delegate_task``。

        返回:
            已注入正确 tool_executor / model_tools / execution_context / trace_recorder 的
            WorkflowOperations 实例。
        """
        task_space = task_runtime_spaces.get_or_create(task.id)
        allows_tools = {tool.get("name") for tool in task_space.task_tool_definitions}

        ban_tools = set(run.extra.ban_tools if run.extra is not None else ())
        if run.extra is not None and len(run.extra.ban_tools) > 0:
            allows_tools = allows_tools - ban_tools
            task_space.defer_system_message(
                SystemMessage(
                    content=(
                        f"Tools banned for this round: {ban_tools}. These tools cannot be "
                        "executed in this round; please do not use them."
                    )
                )
            )
        else:
            task_space.defer_system_message(
                SystemMessage(content="All tools are allowed for this round.")
            )

        if run.extra is not None and run.extra.propose_agent_configuration:
            allows_tools.add(TOOL_PROPOSE_AGENT_CONFIGURATION)
            definition: ToolDefinition = self._tool_register.get_tool_definition(
                TOOL_PROPOSE_AGENT_CONFIGURATION
            )
            tool_schema = convert_to_openai_tool(definition.to_model_tool_definition(), strict=True)
            task_space.defer_system_message(
                SystemMessage(
                    content=(
                        "Please follow the user's instructions and use this tool to carry out "
                        f"their request: {tool_schema}"
                    )
                )
            )

        execution_context = self._resolve_execution_context(task, run_id=run.id)
        runtime_dependencies = None
        if execution_context is not None:
            runtime_dependencies = ToolRuntimeDependencies(
                parent_agent_profile=agent_profile,
                agent_profile_registry=get_agent_registry(),
                terminal_session_service=get_terminal_session_service(),
                is_run_cancelled=cancellation_registry.is_cancelled,
                process_tool_output_channel_factory=self._process_tool_output_channel_factory,
            )
        return WorkflowOperations(
            tool_executor=self._tool_executor,
            agent_profile=agent_profile,
            current_run=run,
            current_task=task,
            current_workspace=workspace,
            allows_tools=allows_tools,
            execution_context=execution_context,
            runtime_dependencies=runtime_dependencies,
            tool_trace_recorder=tool_trace_recorder,
        )
