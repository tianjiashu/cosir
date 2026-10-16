"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译包含 ``model`` / ``tools`` / ``wait_user`` /
``observe`` / ``structured_output`` 的 graph，以 LangGraph 状态流驱动执行；节点产生的模型、
工具和终态事实由 ``WorkflowOperations`` 写入 canonical conversation state，Transport 只订阅该事实。
graph 编译时挂既有 checkpointer，由 LangGraph 负责控制流状态持久化。

协作取消与用户输入等待通过 LangGraph ``interrupt`` 保留图断点；其中 human-in-the-loop 根据工具观察
从 ``tools`` 条件路由到 ``wait_user``，批准后回 ``tools`` 真执行，驳回 / 放弃后交给 ``observe``；
用户的单个结构化决定经续跑入口传入并被 ``wait_user`` 消费；待决请求
投影给前端与挂起图都发生在 ``wait_user``。终端属于 Task，由终端服务独立管理，
不进入 graph state，也不随 Run 完成或取消关闭。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

from time import perf_counter
from typing import Any, cast

from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config.configuration import get_tool_system
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.runtime.execution_mode import ExecutionMode
from app.core.tools.schemas import UserDecision, build_resume_payload
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.workflow_operations import WorkflowOperations

from ..agent_workflow import AgentWorkflow, WorkflowRunFailure, build_checkpointer
from .edges import _route_target
from .node_helper.request_fingerprint import log_tools_fingerprint
from .runtime_config import RuntimeConfig
from .worflow_state.route import ReactRoute


class ReactLikeWorkflow(AgentWorkflow):
    """基于"模型推理 -> 工具调用 -> 继续推理/最终回答"的默认工作流，由 LangGraph 编排。

    本类承担执行策略与运行期装配：按 Agent profile 自解析模型并绑定工具 schema、按 Task 取
    ``RuntimeContextManager`` 写本 Run 的 canonical 消息、自开 checkpointer 编译 graph；工具执行与
    Run 状态迁移经 ``WorkflowOperations`` 门面。Run 终态由节点在收束时落定，异常原样交给运行时收敛。
    """

    workflow_id = "react_like_v1"

    def __init__(self) -> None:
        """初始化工作流；本类不持有运行期状态，依赖都在 :meth:`run` 时经入参与门面注入。"""

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        ``model`` / ``tools`` / ``observe`` 三节点经条件边形成 ReAct 循环；工具结果带待决用户请求时
        才进入 ``wait_user``，否则直接由 ``observe`` 收口。用户批准后回到 ``tools`` 真正执行。
        graph 编译时挂入 ``checkpointer`` 以启用 graph 控制流持久化。普通协作取消仍由 ``model``
        节点的 ``interrupt`` 中断。

        参数:
            checkpointer: 已配置好的 LangGraph checkpointer。

        返回:
            已编译的 StateGraph。
        """

        # 延迟导入节点，打破 nodes 子包与 react 包之间的循环导入：
        # nodes.model_node -> react.state/runtime_config -> react.__init__
        # -> react.workflow -> nodes
        from app.core.workflows.react.nodes import (
            _model_node,
            _observe_node,
            _structured_output_node,
            _tools_node,
        )
        from app.core.workflows.react.nodes.wait_user_node import wait_user_node

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _model_node)
        builder.add_node("tools", _tools_node)
        builder.add_node("wait_user", wait_user_node)
        builder.add_node("observe", _observe_node)
        builder.add_node("structured_output", _structured_output_node)
        builder.add_edge(START, "model")
        # 超配额拦截收口在 model 节点（发起推理前 step_count > max_steps 直接终态）。
        builder.add_conditional_edges(
            "model",
            _route_target,
            {
                ReactRoute.TOOLS.value: "tools",
                ReactRoute.MODEL.value: "model",
                ReactRoute.STRUCTURED_OUTPUT.value: "structured_output",
                END: END,
            },
        )
        builder.add_edge("structured_output", END)
        # tools 根据结果中的待决请求选择等待用户，违规 HIL 批次与普通结果直接交给 observe。
        builder.add_conditional_edges(
            "tools",
            _route_target,
            {
                ReactRoute.WAIT_USER.value: "wait_user",
                ReactRoute.OBSERVE.value: "observe",
            },
        )
        # 用户批准后回 tools 真正执行；驳回 / 放弃交给 observe 收口。空恢复值时自环，
        # 使 LangGraph 在新的节点执行中重新挂起同一请求。
        builder.add_conditional_edges(
            "wait_user",
            _route_target,
            {
                ReactRoute.TOOLS.value: "tools",
                ReactRoute.OBSERVE.value: "observe",
                ReactRoute.WAIT_USER.value: "wait_user",
            },
        )
        # observe 判定后回 model 或结束工作流；工具结果观察的唯一收口在此。
        builder.add_conditional_edges(
            "observe",
            _route_target,
            {
                ReactRoute.MODEL.value: "model",
                END: END,
            },
        )
        return builder.compile(checkpointer=checkpointer)

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
        user_decision: UserDecision | None = None,
    ) -> None:
        """驱动 ReAct graph 执行一次任务，直到完成、挂起或异常终止。

        以 LangGraph 状态流驱动已编译 graph：模型解析与工具绑定、运行期配置注入、上下文基线
        补写、graph 启动与流式消费都在本方法内完成。Task 终端由独立服务管理，
        不随图的退出释放。本工作流不生产或透传通用 runtime event；Run 状态迁移与工具执行
        经 ``WorkflowOperations`` 提交，canonical
        消息由本方法装配的 ``RuntimeContextManager`` 写入，模型流式增量由 graph custom stream 经
        ``operations.process_event`` 转发到 snapshot。协作取消与通用用户输入等待都通过 LangGraph
        ``interrupt`` 保存 checkpoint。

        本方法**不收口异常**：模型解析失败与「续跑时 checkpoint 已结束」抛带稳定失败码的
        ``WorkflowRunFailure``，其余异常原样传播，统一由唯一调用方 ``AgentRuntime.run_agent``
        分类并落定 Run failed 终态；取消信号（``CancelledError``）不被捕获，继续向上走
        cancelled 路径。

        参数:
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新。
            callbacks: 可选 LangChain callbacks（如 Langfuse ``CallbackHandler``），注入
                ``graph.astream`` 的 ``config["callbacks"]`` 使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选 Langfuse trace 标识；由 runner 在启用 tracing 时注入。
                未启用 Langfuse 时为 None。
            execution_mode: 本次执行是 ``fresh`` 还是 ``resume`` / ``resume_with_input``；它
                决定注入 graph 的输入（``fresh`` 传初始 state；``resume`` 传
                ``Command(goto="model")`` 回退到 model 节点重跑，``resume_with_input`` 在
                ``wait_user`` 处于 interrupt 时传 ``Command(resume=...)``）以及上下文是否
                清空该 run 的旧条目（见 ``RuntimeContextManager.begin_run``）。取消一个停在
                ``wait_user`` 断点上的 Run 走 ``resume_with_input``：取消入口构造强行拒绝决定，
                让断点被消费而不是被绕过。
            user_decision: 本次续跑携带的用户结构化决定；``resume_with_input`` 下若恢复现场的
                图状态里存在处于 interrupt 的任务，就把它作为 ``Command(resume=...)`` 的载荷注入
                （当前只有 ``wait_user`` 会把 Run 置为等待用户输入）。为空时注入空决定，由
                ``wait_user`` 重新挂起等待用户输入。取消等待中的 Run 由 ``ConversationRunExecutor``
                注入系统合成的强行拒绝决定（``FORCE_REJECT``），由 ``wait_user`` 归一为驳回。

        返回:
            无。图正常收束或在等待节点处挂起后返回，interrupt 与 checkpoint 由 LangGraph 持久化。

        异常:
            WorkflowRunFailure: 运行期模型解析失败、checkpoint 已结束，或带用户决定续跑时找不到
                对应的 ``wait_user`` interrupt。
            Exception: 上下文写入失败、graph 构建 / 执行失败时原样传播，由
                ``AgentRuntime.run_agent`` 的异常边界落定 Run failed 终态。
            asyncio.CancelledError: 不被捕获，保留取消语义向上传播。

        副作用:
            解析并绑定模型；构造运行期配置与 task 级上下文管理器并补写本 Run 的 user 基线
            消息；开启 checkpointer 编译 graph 并流式驱动。终端进程收尾由执行器在 Run 状态收敛后
            调用 ``finalize_run``，因此等待输入返回不会触发终态清理。
        """

        run = operations.get_current_run()
        run_id = run.id
        current_task = operations.get_current_task()

        agent_profile = operations.agent_profile
        try:
            resolved_model = resolve_chat_model(agent_profile=agent_profile)
            base_model = resolved_model.model
        except Exception as exc:
            raise WorkflowRunFailure(
                Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE,
                "运行期模型解析失败",
            ) from exc
        # 绑定的是 Task 创建时固化的完整模型 schema。禁用工具只由
        # ToolCallLifecycleManager 按本 Run 的 allows_tools 拦截，不能据此过滤 bind_tools；
        # 否则同一 Task 的模型工具定义会随 Run 改变，破坏前缀缓存复用。
        tool_schemas = operations.task_tool_schemas
        # 工具 schema 按 Run 记录指纹：它是本 Run 的常量，跨 Run 变化即说明工具定义被改动，
        # 缓存会从工具段起失效。取自身下 bind_tools 的同一份 schema，保证指纹与实际下发一致。
        log_tools_fingerprint(tool_schemas, task_id=current_task.id, run_id=run_id)
        if len(tool_schemas) > 0:
            bound_model = base_model.bind_tools(
                tool_schemas,
                strict=True,
                parallel_tool_calls=True,
            )
        else:
            bound_model = base_model

        thinking_channel = "reasoning_content" if resolved_model.supports_thinking else ""

        runtime_config = RuntimeConfig(
            operations=operations,
            run=run,
            model=cast(Runnable, bound_model),
            structured_output=agent_profile.structured_output,
            start_time=perf_counter(),
            usage_stats=ConversationRunUsageStats(),
            langfuse_trace_id=langfuse_trace_id,
            thinking_channel=thinking_channel,
            execution_mode=execution_mode,
            user_input_decision_handlers=get_tool_system().user_input_decision_handlers,
        )
        # task 级上下文管理器是 Agent 上下文的唯一运行时协调入口：首次执行时惰性创建，并由
        # task space 以弱引用缓存——本方法必须持有它直到 graph 执行结束，否则实例会被回收，
        # 下一次访问只能从持久化 context 重新加载。
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        runtime_context_manager = task_runtime_spaces.get_or_create(
            current_task.id
        ).get_context_manager(agent_profile=agent_profile)

        # 绑定本 Run：fresh 只按 run 清掉该 Run 的旧条目（同 Task 的历史条目保留，作为模型输入的
        # 前缀），resume 保留该 Run 已有条目。
        runtime_context_manager.begin_run(
            run,
            execution_mode,
        )
        # 本 Run 的初始 user 消息是 canonical 上下文事实，必须在 graph 启动前补齐：fresh 时
        # ``begin_run`` 已清空该 Run 的旧条目，这里写基线；resume 时该 Run 的消息通常已存在。
        # 是否「已存在」由方法内部按 canonical context 事实判定，**不能**用 ``execution_mode``
        # 代替：续跑在「上次崩于写入之前」时仍需补写。写入失败由异常向上收敛为 Run 失败。
        runtime_context_manager.ensure_run_user_message(
            run.input_text,
            run.image_paths,
            run.extra.display_text if run.extra is not None else run.input_text,
            run.extra.attachments if run.extra is not None else None,
        )

        config = {
            "configurable": {
                "thread_id": run.checkpoint_thread_id,
                "run_id": run_id,
                "runtime_config": runtime_config,
                # 与 runtime_config 同口径经 config 注入，不进入 graph state
                # （非 list 对象不兼容 state reducer）。
                "runtime_context": runtime_context_manager,
            },
            # LangChain callbacks（如 Langfuse CallbackHandler）经此注入模型调用追踪。
            "callbacks": callbacks or [],
        }

        async with build_checkpointer() as checkpointer:
            graph = self._build_graph(checkpointer)
            # 初始 state 只填控制流字段：模型消息与 runtime context 都不进 state（前者归
            # RuntimeContextManager，后者经 config 注入）。
            input_state = ReactGraphState(
                step_count=0,
                tool_error_count=0,
                next_node=ReactRoute.MODEL,
                instruction="",
                max_steps=agent_profile.max_steps,
                final_text="",
                last_tool_results={},
            )
            if execution_mode == "resume" or execution_mode == "resume_with_input":
                # 续跑准入：必须先判图状态。图已走到 END 时 ``astream`` 既不产出事件也不
                # 返回（协程永久挂起），因此必须先拒绝续跑；失败码随异常交给运行时收敛。
                snapshot = await graph.aget_state(config)
                if not snapshot.next:
                    raise WorkflowRunFailure(
                        Constant.Run.RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED,
                        "该 Run 的 checkpoint 已结束，无法续跑",
                    )
                # 决定只能恢复 wait_user 自己建立的 interrupt；其它节点的 interrupt 不得误收决定。
                parked_at_wait_user = any(
                    task.name == "wait_user" and task.interrupts for task in snapshot.tasks
                )
                log.info(
                    "workflow_resume_checkpoint_inspected",
                    extra={
                        "msg": "已检查续跑 checkpoint 的待执行节点与 interrupt",
                        "data": {
                            "run_id": run_id,
                            "execution_mode": execution_mode,
                            "next_nodes": list(snapshot.next),
                            "interrupt_nodes": [
                                task.name for task in snapshot.tasks if task.interrupts
                            ],
                            "user_decision_present": user_decision is not None,
                        },
                    },
                )
                if execution_mode == "resume_with_input":
                    runtime_config.resuming_wait_user = parked_at_wait_user
                    if user_decision is not None and not parked_at_wait_user:
                        log.error(
                            "workflow_resume_decision_interrupt_missing",
                            extra={
                                "msg": "收到用户决定，但 checkpoint 中没有 wait_user interrupt",
                                "data": {
                                    "run_id": run_id,
                                    "next_nodes": list(snapshot.next),
                                    "interrupt_nodes": [
                                        task.name for task in snapshot.tasks if task.interrupts
                                    ],
                                },
                            },
                        )
                        raise WorkflowRunFailure(
                            Constant.Run.RUN_FAILURE_CODE_WAITING_FOR_INPUT,
                            "用户决定无法应用：工作流已不在等待用户决定的节点",
                        )
                    input_state = None
                    if parked_at_wait_user:
                        input_state = Command(resume=build_resume_payload(user_decision))
                else:
                    # 图停在 human-in-the-loop 断点上却没有任何决定：``goto model`` 会绕开
                    # ``wait_user`` 把待决请求静默丢掉，并让模型读到「等待确认」的占位结果行。
                    # 因此显式失败，由调用方改用带决定的续跑——取消等待中的 Run 由取消入口构造
                    # 强行拒绝决定，同样走带决定的入口，不经过这条判定。
                    if parked_at_wait_user:
                        raise WorkflowRunFailure(
                            Constant.Run.RUN_FAILURE_CODE_WAITING_FOR_INPUT,
                            "该 Run 正在等待用户决定，请提交决定后续跑",
                        )
                    # 无用户决定的续跑回到 model；Task 终端独立存活，不改写图的恢复状态。
                    input_state = Command(goto=ReactRoute.MODEL.value)

            async for mode, value in graph.astream(
                input_state,
                config,
                stream_mode=["values", "custom"],
            ):
                # values 只推进图；custom 携带模型 chunk 的中性增量，由本工作流
                # 统一写入 snapshot。两者都不是 Agent context 的来源。
                if mode != "custom":
                    continue
                operations.process_event(value)
