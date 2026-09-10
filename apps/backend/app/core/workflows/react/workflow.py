"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译 graph（``model`` / ``tools`` / ``observe`` 节点 +
条件边），以 LangGraph 状态流驱动图执行；节点产生的模型、工具和终态事实由
``RuntimeOperations`` 写入 canonical conversation state，Transport 只订阅该事实。
graph 编译时挂既有 checkpointer，由 LangGraph 负责控制流状态持久化。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

from time import perf_counter
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from app.config.logging.logger import log
from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.runtime.checkpointer import build_checkpointer
from app.core.runtime.execution_mode import ExecutionMode
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.workflow_operations import WorkflowOperations
from app.service.provider.capability_service import CapabilityService

from ...context.runtime_context_manager import RuntimeContextManager
from ..agent_workflow import AgentWorkflow
from .edges import _after_observe, _after_tools, _should_continue
from .runtime_config import RuntimeConfig
from .state import ReactGraphState


class ReactLikeWorkflow(AgentWorkflow):
    """基于“模型推理 -> 工具调用 -> 继续推理/最终回答”的默认工作流，由 LangGraph 编排。

    该类只承担执行策略职责，不直接创建模型、工具或数据库连接，所有外部能力都通过
    ``RuntimeOperations`` 注入。graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph
    负责控制流状态持久化。
    """

    workflow_id = "react_like_v1"

    def __init__(self) -> None:
        """初始化工作流。

        参数:
            无。
        """

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        ``model`` / ``tools`` / ``observe`` 三节点经条件边形成 ReAct 循环；graph 编译时挂入
        ``checkpointer`` 以启用 graph 控制流持久化。

        参数:
            checkpointer: 已配置好的 LangGraph checkpointer。

        返回:
            已编译的 StateGraph。
        """

        # 延迟导入节点，打破 nodes 子包与 react 包之间的循环导入：
        # nodes.model_node -> react.state/runtime_config -> react.__init__
        # -> react.workflow -> nodes
        from ..nodes import _model_node, _observe_node, _tools_node

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _model_node)
        builder.add_node("tools", _tools_node)
        builder.add_node("observe", _observe_node)
        builder.add_edge(START, "model")
        # 超配额拦截收口在 model 节点（发起推理前 step_count > max_steps 直接终态）。
        builder.add_conditional_edges(
            "model",
            _should_continue,
            {"tools": "tools", "model": "model", END: END},
        )
        # tools 执行后进入 observe；取消/终态分支直接 END，不进 observe 避免多余推理。
        builder.add_conditional_edges("tools", _after_tools, {"observe": "observe", END: END})
        # observe 判定后回 model 继续推理，或达错误上限终态 END。
        builder.add_conditional_edges("observe", _after_observe, {"model": "model", END: END})
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def _write_stream_item(operations: WorkflowOperations, mode: str, value: object) -> None:
        """消费 workflow stream 中的模型增量并写入 snapshot。

        参数:
            operations: 当前 Conversation Run 的运行时操作门面。
            mode: LangGraph stream 模式；只有 ``custom`` 模式由本方法处理。
            value: custom stream 产出的中性模型增量。

        返回:
            无。

        异常:
            ValueError: custom stream 增量结构非法。

        副作用:
            将文本或 reasoning 增量交给 ``RuntimeOperations``，由其更新 snapshot 并发布
            Assistant Transport 增量；不会写入 Agent context。
        """

        if mode != "custom":
            return
        operations.process_event(value)

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """执行一个任务，直到完成、失败、取消或达到最大步骤数。

        以 LangGraph 状态流驱动已编译 graph。工作流不再生产或透传 RuntimeEvent；模型、
        工具和终态事实由 ``RuntimeOperations`` 直接提交到 canonical conversation state，
        模型流式增量由 graph custom stream 统一转发到 snapshot。
        模型经 ``resolve_chat_model`` 构建（缺 Key 在构建期抛错）；
        工具由服务端工具策略直接执行，工作流本身不暂停等待外部决策。

        参数:
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新。
            callbacks: 可选 LangChain callbacks（如 Langfuse ``CallbackHandler``），注入
                ``graph.astream`` 的 ``config["callbacks"]`` 使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选 Langfuse trace 标识；由 runner 在启用 tracing 时注入，
                终态事件 payload 会携带该字段供前端展示。未启用 Langfuse 时为 None。

        生成:
            无。该异步迭代器只保留工作流协议的可消费形状，不产生运行时事件。
        """

        run = operations.get_current_run()
        # 一个 Conversation Run 对应一个 LangGraph checkpoint thread。这里必须使用
        # run 创建时持久化的 UUID，而不是自增 run.id：主库可被清空/重建并重新分配同一个
        # 整数 ID，而 checkpoint 库可能仍保留旧状态；UUID 才能保证两套生命周期真正隔离。
        run_id = run.id
        current_task = operations.get_current_task()

        # Agent 执行主体
        agent_profile = operations.agent_profile
        # 构建模型：按 run.model_name 运行期兜底解析（None 时回退 agent_profile.model_name），
        # 失败记 ``model_resolve_failed`` 后抛出，由外层 graph.astream 异常分支收敛为 RUN_FAILED。
        try:
            base_model = resolve_chat_model(
                run=run,
                agent_profile=agent_profile,
            )
        except Exception as exc:
            log.exception(
                "model_resolve_failed",
                extra={
                    "msg": f"运行期模型解析失败，run 进入 RUN_FAILED：{exc}",
                    "data": {
                        "task_id": current_task.id,
                        "run_id": run.id,
                        "model": run.model_name or agent_profile.model_name,
                    },
                },
            )
            raise
        # 构建工具
        tool_schemas = [
            tool.to_model_tool_definition()
            for tool in operations.model_tools
            if agent_profile.allowed_tools is None or tool.name in agent_profile.allowed_tools
        ]
        try:
            bound_model = (
                base_model.bind_tools(tool_schemas, strict=True) if tool_schemas else base_model
            )
        except NotImplementedError:
            # 降级防御：模型不支持 bind_tools 时禁用工具继续运行（与缺 Key 无关，缺 Key 在
            # resolve_chat_model 构建期即抛错，不会走到这里）。
            log.warning(
                "model %s does not support bind_tools; running without tools "
                "(degraded: model does not implement tool binding)",
                type(base_model).__name__,
            )
            bound_model = base_model

        if run.provider_id is None:
            raise ValueError("Conversation Run provider_id is required")
        thinking_channel = CapabilityService.get_thinking_channel(run.provider_id)
        vision_input_format = CapabilityService.get_vision_input_format(run.provider_id)

        runtime_config = RuntimeConfig(
            operations=operations,
            run=run,
            model=cast(BaseChatModel, bound_model),
            start_time=perf_counter(),
            usage_stats=ConversationRunUsageStats(),
            langfuse_trace_id=langfuse_trace_id,
            thinking_channel=thinking_channel,
            thinking_roundtrip=True,
            vision_input_format=vision_input_format,
            execution_mode=execution_mode,
        )
        current_workspace = operations.get_current_workspace()
        # 构造 task 级运行时上下文（唯一事实源），注入 store 端口使 manager 成为消息
        # 读写唯一入口，并挂载上下文占用订阅者。
        runtime_context_manager = RuntimeContextManager.ensure_get_runtime_context_manager(
            agent_profile,
            current_workspace,
            current_task,
        )

        # 每个新 ConversationRun 都从 canonical history 建立 fresh 上下文。
        runtime_context_manager.begin_run(
            run,
            execution_mode,
            tool_schemas=tool_schemas,
        )
        if execution_mode == "fresh":
            # 只有 fresh 才写入新的用户消息；resume 必须保留已有 ContextEntry。
            runtime_context_manager.add_message(HumanMessage(content=run.input_text))
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
            # get_stream_writer() 只在 graph 执行上下文内有效；首条 user message 在
            # graph 建立前写入 context，因此 emitter 必须延迟到此处绑定。
            initial_state = ReactGraphState(
                step_count=0,
                tool_error_count=0,
                requested_tool=False,
                repair_requested=False,
                continuation_error_data=None,
                final_response=False,
                terminal=False,
                pending_tool_calls={},
                deferred_repair_message="",
                max_steps=agent_profile.max_steps,
                final_text="",
                last_tool_results={},
            )
            # None 是 LangGraph 从既有 checkpoint 继续的明确语义；新的 dict 会启动
            # 一个新的 graph input，即使 thread_id 相同也不等价于 resume。
            input_state: ReactGraphState | None = (
                initial_state if execution_mode == "fresh" else None
            )
            while True:
                try:
                    async for mode, value in graph.astream(
                        input_state,
                        config,
                        stream_mode=["values", "custom"],
                    ):
                        # values 只推进图；custom 携带模型 chunk 的中性增量，由本工作流
                        # 统一写入 snapshot。两者都不是 Agent context 的来源。
                        self._write_stream_item(operations, mode, value)
                except Exception:
                    log.exception(
                        "workflow_graph_failed",
                        extra={
                            "msg": "langgraph execution failed during workflow run",
                            "data": {
                                "task_id": current_task.id,
                                "run_id": operations.get_current_run().id,
                            },
                        },
                    )
                    raise

                state_snap = await graph.aget_state(config)
                tasks = state_snap.tasks
                if not tasks:
                    break
                break
