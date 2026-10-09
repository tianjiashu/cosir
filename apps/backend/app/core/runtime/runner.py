"""Agent 执行的运行时引擎，也是 workflow 的调用与异常收敛边界。

本模块是 workflow 的唯一组织者：解析本次 Run 的 Agent profile、装配 ``WorkflowOperations`` 门面、
触发运行期 Hook、驱动 ``workflow.run``，并收敛 workflow 逃逸的异常。模型流消费、工具调度与节点
路由等执行细节都在具体 workflow 内，本模块不感知。

不负责：Run 的正常终态（completed / cancelled / 等待用户输入）由 workflow 节点落定；checkpoint
读写由 workflow 持有的 checkpointer 负责；任务 / 工作区 / 轮次的 CRUD 与查询委托对应 service。
"""

import asyncio
from collections.abc import Sequence

from langchain_core.messages import SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.config.configuration import get_agent_registry, get_tool_system
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.model_settings import ModelSettings
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
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
from app.core.tools.schemas import ToolDefinition, ToolExecutionContext, UserDecision
from app.core.tools.schemas.tool_names import TOOL_PROPOSE_AGENT_CONFIGURATION
from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.workflows.agent_workflow import WorkflowRunFailure
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models import (
    ConversationRunRecord,
    ConversationRunStatus,
    TaskRecord,
    WorkspaceRecord,
)
from app.models.conversation_run_failure import run_failure_message
from app.service.depends import (
    get_conversation_run_observability_service,
    get_conversation_run_state_service,
    get_model_config_service,
    get_task_service,
    get_terminal_session_service,
    get_workspace_service, get_conversation_run_service,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class AgentRuntime:
    """执行与生命周期引擎：驱动 workflow，并把执行边界收口在这一层。

    状态单一事实来源是 ``ConversationRun``：本引擎只推进 run 执行态与异常终态。协作取消作用于
    run 而非直接杀协程——由 workflow 节点在检查点处观察取消信号后停止派发（见 ``model_node``）。

    职责边界：
    - 负责：解析 per-run Agent profile、装配 ``WorkflowOperations``、触发 USER_PROMPT_SUBMIT /
      STOP Hook、驱动 ``workflow.run``、把逃逸异常分类后落定 Run failed 终态并记 ``task_failed``、
      结束前回收进程内取消信号。
    - 不负责：模型流消费、工具调度与节点路由（在 workflow 内）；Run 正常终态（由 workflow 节点
      落定）；checkpoint 读写（由 workflow 持有的 checkpointer 负责）；checkpoint 回放与历史事件
      回看（由 Conversation service 提供）；工作区 / 任务 / 轮次的 CRUD 与查询（委托给对应
      service 层）。不对外暴露 service 访问器，service 仅作为私有协作者。
    """

    def __init__(
            self,
            *,
            process_tool_output_channel_factory: (ProcessToolOutputChannelFactory | None) = None,
    ) -> None:
        """解析并持有本引擎的私有协作者。

        依赖（任务 / 工作区 / run 状态 service、工具执行器与注册表、Agent 目录）全部经进程级
        依赖入口解析，不接收入参。唯一例外是进程工具输出通道工厂——由应用装配入口注入，避免核心
        runtime 依赖 Assistant Transport。工具执行器与注册表在构造期固定，配置变更后需要重装配时
        重建 ``AgentRuntime``。

        参数:
            process_tool_output_channel_factory: 可选的进程工具输出通道工厂；缺省时不投递进程
                工具的运行期输出事件。

        异常:
            RuntimeError: 依赖入口要求的存储或配置尚未初始化时抛出。
        """

        self._task_service = get_task_service()
        self._tool_executor = get_tool_system().executor
        self._tool_register = get_tool_system().registry
        self._agent_registry = get_agent_registry()
        self._workspace_service = get_workspace_service()
        self._process_tool_output_channel_factory = process_tool_output_channel_factory
        self._conversation_run_state_service = get_conversation_run_state_service()

    def resolve_agent_profile_for_run(self, run: ConversationRunRecord) -> "AgentProfile":
        """解析并派生单次 run 独占的 agent profile。

        workspace 作用域按 Agent 身份选取：主 Agent 用系统作用域（系统 Agent 不属于任何工作区），
        其余用该 Run 所属工作区的 root_path。模型设置只在 Run 显式绑定 ``model_config_id`` 时从
        配置覆盖，未绑定时沿用 profile 上的默认设置。

        参数:
            run: 已被执行器认领的 Conversation Run 记录。

        返回:
            经 :meth:`AgentProfile.derive_for_run` 派生的 per-run 副本；共享注册表单例不被原地写，
            并发 run 互不串扰。

        异常:
            RuntimeError: registry 解析不到该 Agent 时抛出（workspace 未命中且 system 作用域也未
                注册，含子 Agent 配置文件无效时 registry 表现为未命中）；调用方
                ``ConversationRunExecutor`` 在统一收尾中兜住该失败路径。

        副作用:
            只读 task / workspace 记录与 model config；不写任何持久化状态。
        """
        run_id = run.id
        task = self._task_service.get_task(run.task_id)
        workspace = self._workspace_service.get_workspace(task.workspace_id)
        agent_id: str = run.agent_id or "main_agent"
        workspace_scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE if agent_id == "main_agent" else workspace.root_path
        )
        # ``resolve`` 不抛配置异常：workspace 未命中会回退 system 作用域，仍取不到才返回 None
        # （见 ``AgentProfileRegistry`` 的已知缺口），因此这里只有「解析不到」这一条失败路径。
        agent_profile = self._agent_registry.resolve(workspace_scope, agent_id)
        if agent_profile is None:
            raise RuntimeError(f"agent profile unavailable for run {run_id}")
        runtime_model_settings = None
        if run.model_config_id is not None:
            runtime_model_settings = ModelSettings.from_model_config_record(
                get_model_config_service().get_config(run.model_config_id)
            )
        return agent_profile.derive_for_run(run, model_settings=runtime_model_settings)

    async def run_agent(
            self,
            agent: AgentProfile,
            *,
            execution_mode: ExecutionMode = "fresh",
            user_decisions: Sequence[UserDecision] = (),
    ) -> None:
        """驱动一次 agent run 执行，并作为 workflow 异常的收敛边界。

        本方法是「运行时 ↔ 工作流」的唯一驱动入口：装配门面 → 驱动 workflow → 分类并落定异常
        终态 → 回收进程内取消信号。它不落定正常终态，那些由 workflow 节点在收束时完成。

        参数:
            agent: 当前 run 的 Agent profile。**必须是 per-run 派生副本**（经
                ``AgentProfile.derive_for_run`` 派生）；禁止传入共享注册表单例，
                run 执行期间可安全写入副本上的运行时字段（如 ``main_agent``），
                不污染共享实例。
            execution_mode: 本次执行是 ``fresh`` / ``resume`` / ``resume_with_input``，原样透传给
                ``agent.workflow.run``。
            user_decisions: 本次续跑携带的用户结构化决定（human-in-the-loop），原样透传给
                ``agent.workflow.run``；创建 / 编辑路径为空序列。

        异常:
            RuntimeError: ``agent.run`` 为 None（profile 未绑定 run）时抛出。
            asyncio.CancelledError: 不被 ``except Exception`` 捕获，取消语义继续向上传播。
            其余异常不上抛：workflow 逃逸的异常在本方法的异常边界内分类并落定 failed 终态，
            随后就地收敛（执行器的未落终态兜底对已落终态的 run 是 no-op）。

        副作用:
            触发 USER_PROMPT_SUBMIT（认领成功后）与 STOP（正常收束 / 取消 / 挂起）Hook；异常终态经
            run 状态服务条件更新落定并记 ``task_failed`` 日志；结束前清理进程内取消信号。本轮消息
            落库、canonical conversation facts 与快照收口由 ``workflow.run`` 内的
            ``RuntimeContextManager`` 负责。
        """

        if agent.run is None:
            raise RuntimeError("agent profile unavailable for run")

        run = agent.run
        run_id = run.id
        task_id = run.task_id
        task = self._task_service.get_task(task_id)
        workspace = self._workspace_service.get_workspace(task.workspace_id)
        # UserPromptSubmit 挂接：本轮已被成功认领后触发。即使 Hook 判定 deny 也不阻断主流程
        # （run 已认领，此时硬中断要额外收敛终态，侵入面过大）；无内置实现，空订阅下 fire 零开销。

        await HookInterceptor.async_safe_fire(
            HookContext.from_locatable(event=HookEvent.USER_PROMPT_SUBMIT, turn=run, locatable=None)
        )
        # 门面在 try 内构造，异常边界因此可能拿不到它；这里先声明为 None，仅用于在
        # ``task_failed`` 日志里区分「workflow 执行失败」与「门面尚未装配就失败」。
        operations: WorkflowOperations | None = None
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
                # 本轮消息轨迹（清空残留、落 user 基线、逐条增量落库）统一由 workflow.run 内的
                # RuntimeContextManager 负责，runner 不再直接落库。
                await agent.workflow.run(
                    operations,
                    callbacks=trace_result.callbacks,
                    langfuse_trace_id=trace_result.trace_id,
                    execution_mode=execution_mode,
                    user_decisions=user_decisions,
                )
            # Langfuse recorder 使用 SDK 自带的后台批量上报。不能在对话收尾路径
            # 主动调用同步 flush：网络不可用时 SDK 会等待重试，导致 run 无法及时
            # 进入 completed/failed 终态，前端会一直显示运行中。
            current_run = await asyncio.to_thread(
                get_conversation_run_service().get_run,
                run_id,
            )
            if current_run.status in {
                ConversationRunStatus.COMPLETED.value,
                ConversationRunStatus.CANCELLED.value,
                ConversationRunStatus.WAITING_FOR_INPUT.value,
            }:
                # Stop 挂接：仅当 canonical 状态显示工作流正常收束 / 取消 / 挂起时触发。异常路径
                # 不会走到这里（异常分支已落定 failed），因此失败不会被伪装成正常结束。
                await HookInterceptor.async_safe_fire(
                    HookContext.from_locatable(
                        event=HookEvent.STOP,
                        locatable=task,
                        turn=run,
                    )
                )
        except Exception as exc:
            failure_code = self._failure_code_for(exc)
            # 落定走 run 级状态服务，与门面是否构造成功无关；run 已落终态（完成 / 取消 / 等待用户
            # 输入）时条件更新落空，不会覆盖 workflow 落定的既有终态。
            self._conversation_run_state_service.fail_run_if_running(
                run_id,
                end_reason=failure_code,
                final_output=run_failure_message(failure_code),
                error_message=extract_model_response_message(exc),
            )
            log.exception(
                "task_failed",
                extra={
                    "msg": f"run 执行失败，失败 code: {failure_code}，错误类型: {type(exc).__name__}",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "failure_code": failure_code,
                        "error_type": type(exc).__name__,
                        "settled": operations is not None,
                    },
                },
            )
        finally:
            cancellation_registry.clear(run_id)
            # 工具级信号由工具执行层在单次调用结束时释放；这里兜底回收「点名了已结束的
            # 工具调用」这类不会再被消费的信号，避免进程内信号随会话累积。
            tool_call_cancellation_registry.clear_run(run_id)

    @staticmethod
    def _failure_code_for(exc: BaseException) -> str:
        """把异常归类为 Run 失败 code；识别不出模型调用错误时用中性兜底 code。

        工作流已经判定过失败语义的异常（``WorkflowRunFailure``）自带 code，直接用它，不再
        二次猜测；其余异常交给模型失败分类器，分类不出时回退中性 code，避免把普通执行错误
        误报成模型问题。

        参数:
            exc: 待归类的异常。可能来自 workflow 执行期、graph 构建期、门面构建期或
                trace 装配期。

        返回:
            ``ErrorKind`` 的模型错误分类值，或 ``Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED``
            （无法判定时）。
        """

        if isinstance(exc, WorkflowRunFailure):
            return exc.failure_code
        return classify_model_failure(exc) or Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED

    def _resolve_execution_context(
            self,
            task: TaskRecord,
            run_id: int = 0,
    ) -> ToolExecutionContext | None:
        """按 task 解析其所属 workspace 的执行上下文；workspace 缺失时返回 None。

        工具运行期依赖（Agent 目录、终端会话、取消判定、进程工具输出通道）随本上下文一起透传，
        因此 workspace 缺失时整个依赖对象为空，破坏性工具与子 Agent 委派都不可用。

        参数:
            task: 当前执行的任务记录；提供 ``workspace_id`` 与 ``task_id``。
            run_id: 当前执行所属轮次标识，供工具执行上下文和取消边界使用；缺省为 0。

        返回:
            命中 workspace 时返回 ToolExecutionContext；workspace 不存在时返回 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库层异常原样冒泡，由 ``run_agent`` 的异常边界
                落定 failed 终态并记 ``task_failed``，不做静默降级。

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
        """为单个 run 构建运行时操作门面，并算出本轮的工具准入边界。

        本方法不改 Task 固化的工具定义，只产出本轮 ``allows_tools``：以 Task 固化工具名为基准集合，
        减去本 Run 的 ``ban_tools``、加上 ``propose_agent_configuration`` 打开时的提案工具。该集合
        同时作用于两处——``WorkflowOperations.task_tool_schemas`` 据此过滤下发给模型的 schema，
        工具执行准入也据此拦截；因此本轮禁用集合 / 提案开关会改变模型看到的工具列表，进而击穿同一
        Task 的前缀缓存（这与 ``docs/plan/conversation-disabled-tool-groups-plan.md`` 第 3 节
        「稳定 bind_tools」的设计意图不一致，属当前代码事实）。workspace 可见性由
        ``execution_context`` 负责，是另一道独立边界。

        参数:
            workspace: 当前 run 所属的 workspace 记录，用于解析工具执行边界。
            task: 当前执行的任务记录（已预取，提供 ``workspace_id`` 与 ``task_id``）。
            run: 当前执行的 Conversation Run 记录（提供 ``run_id`` 作为门面绑定）。
            agent_profile: 驱动本轮执行的 agent profile。
            tool_trace_recorder: 可选的工具调用 trace 记录器（依赖倒置）；为 None 时
                工具执行不产生 trace，行为与集成前一致。

        返回:
            已注入 tool_executor / allows_tools / execution_context / runtime_dependencies /
            trace_recorder 的 WorkflowOperations 实例。

        副作用:
            向本 Task 的延迟系统消息队列投递本轮工具准入说明（禁用集合或「全部放行」，
            ``propose_agent_configuration`` 打开时追加该工具的 schema 说明），由下一次 model
            节点入口消费并写入上下文；这些消息在队列中等待，模型节点不消费则本轮不会进入模型请求。
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
