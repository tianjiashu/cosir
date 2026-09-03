"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译 graph（``model`` / ``tools`` / ``observe`` 节点 +
条件边），以 LangGraph 状态流驱动图执行；节点产生的模型、工具和终态事实由
``RuntimeOperations`` 写入 canonical conversation state，Transport 只订阅该事实。
graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph 负责状态持久化、断点续跑与
审批中断。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

from collections.abc import Callable
from time import perf_counter
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config.logging.logger import log
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.runtime.checkpointer import build_checkpointer
from app.core.workflows.nodes.helper.vision_content_blocks import (
    build_user_content_blocks,
)
from app.models import RuntimeMessage
from app.models.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.errors.llm_provider_exceptions import (
    VisionFormatNotSupportedError,
    VisionImageError,
    VisionNotSupportedError,
)
from app.service.provider.capability_service import CapabilityService
from app.tools.schemas import ToolCall
from app.utils.image_utils import is_image_path

from ...runtime.runtime_operations import RuntimeOperations
from ..agent_workflow import AgentWorkflow
from ..nodes.helper.approval import APPROVAL_INTERRUPT_KEY
from .edges import _after_observe, _after_tools, _should_continue
from .runtime_config import RuntimeConfig
from .state import ReactGraphState


class ReactLikeWorkflow(AgentWorkflow):
    """基于“模型推理 -> 工具调用 -> 继续推理/最终回答”的默认工作流，由 LangGraph 编排。

    该类只承担执行策略职责，不直接创建模型、工具或数据库连接，所有外部能力都通过
    ``RuntimeOperations`` 注入。graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph
    负责状态持久化、断点续跑与 ``interrupt()`` 审批中断。
    """

    workflow_id = "react_like_v1"

    def __init__(
        self,
        approval_resolver: Callable[[list[ToolCall]], list[ToolCall]] | None = None,
    ) -> None:
        """初始化工作流。

        参数:
            approval_resolver: 工具审批解析器；``None`` 表示自动放行全部调用。
                详见类 ``run`` 方法中 ``interrupt()`` 暂停与 ``Command(resume=)`` 恢复逻辑。
        """

        self._approval_resolver = approval_resolver

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        ``model`` / ``tools`` / ``observe`` 三节点经条件边形成 ReAct 循环；graph 编译时挂入
        ``checkpointer`` 以启用 checkpoint 持久化与 ``interrupt`` 恢复。

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

    async def run(
        self,
        operations: RuntimeOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
    ) -> None:
        """执行一个任务，直到完成、失败、取消或达到最大步骤数。

        以 LangGraph 状态流驱动已编译 graph。工作流不再生产或透传 RuntimeEvent；模型、
        工具和终态事实由 ``RuntimeOperations`` 直接提交到 canonical conversation state。
        模型经 ``resolve_chat_model`` 构建（缺 Key 在构建期抛错）；
        存在 ``approval_resolver`` 时 ``tools`` 节点触发 ``interrupt()`` 暂停，用审批解析器解析出
        批准的工具调用并经 ``Command(resume=)`` 恢复；为 ``None`` 时不暂停、自动放行。循环恢复
        graph 直到无待处理任务或工作流结束。

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
        # 一个 Conversation Run 对应一个 LangGraph checkpoint thread；当前物理
        # 迁移阶段 run 仍由 conversation_commands.id 承载，因此这里使用 run 的稳定主键，
        # 而不是 task_id（同一 task 可以拥有多个 run）。
        run_id = run.id
        current_task = operations.get_current_task()

        # Agent 执行主体
        agent_profile = operations.agent_profile
        # 构建模型：按 run.model_name 运行期兜底解析（None 时回退 agent_profile.model_name），
        # 失败记 ``model_resolve_failed`` 后抛出，由外层 graph.astream 异常分支收敛为 RUN_FAILED。
        try:
            base_model = resolve_chat_model(
                task=current_task,
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

        thinking_channel = CapabilityService.get_thinking_channel(run.provider_id)
        vision_input_format = CapabilityService.get_vision_input_format(run.provider_id)

        runtime_config = RuntimeConfig(
            operations=operations,
            run=run,
            model=cast(BaseChatModel, bound_model),
            approval_resolver=self._approval_resolver,
            start_time=perf_counter(),
            usage_stats=ConversationRunUsageStats(),
            langfuse_trace_id=langfuse_trace_id,
            thinking_channel=thinking_channel,
            thinking_roundtrip=True,
            vision_input_format=vision_input_format,
        )
        current_workspace = operations.get_current_workspace()
        # 构造 task 级运行时上下文（唯一事实源），注入 store 端口使 manager 成为消息
        # 读写唯一入口，并挂载上下文占用订阅者。
        runtime_context_manager = RuntimeContextManager.ensure_get_runtime_context_manager(
            agent_profile=agent_profile,
            current_workspace=current_workspace,
            current_task=current_task,
            store=operations.message_store,
            run=run,
        )
        # 每个新 ConversationRun 都从 canonical history 建立 fresh 上下文。
        runtime_context_manager.begin_run(run)

        runtime_context_manager.set_thinking_channel(thinking_channel)

        # run 启动基线：构造带多模态 block 的 user 消息并同时写入当前运行期内存与轨迹；
        # load_history 已排除当前 run，历史回放保持纯文本。图片路径来自 run.image_paths（已在
        # create_run 阶段从附件抽离并落库，仅含图片）；文件/目录/链接已固化进
        # run.input_text，无需在此拼接。视觉格式未实现/聚合超限统一转 VisionNotSupportedError。
        image_paths = [p for p in (run.image_paths or []) if is_image_path(p)]
        try:
            content_blocks, skipped = build_user_content_blocks(
                run.input_text,
                image_paths,
                vision_input_format,
                workspace_root=current_workspace.root_path,
                model_name=run.model_name,
            )
        except (VisionFormatNotSupportedError, VisionImageError) as exc:
            raise VisionNotSupportedError(str(exc)) from exc
        if skipped:
            # 部分图片失效：记 warning 汇总（逐图明细已在 build 内分级记录）
            log.warning(
                "vision_images_partially_skipped",
                extra={
                    "msg": "some images skipped in this run",
                    "data": {
                        "run_id": run.id,
                        "skipped_count": len(skipped),
                        "loaded_count": len(content_blocks) - 1,
                    },
                },
            )
        runtime_context_manager.upsert_current_user_message(
            RuntimeMessage(
                role="user",
                content_text=run.input_text,
                content_blocks=content_blocks if len(content_blocks) > 1 else None,
            ),
            allow_write_event_failure=True,
        )

        config = {
            "configurable": {
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
            initial_state = ReactGraphState(
                step_count=0,
                tool_error_count=0,
                requested_tool=False,
                repair_requested=False,
                continuation_error_data=None,
                final_response=False,
                terminal=False,
                pending_tool_calls=[],
                max_steps=agent_profile.max_steps,
                final_text="",
                last_tool_results=[],
            )
            input_state: ReactGraphState | Command | None = initial_state
            while True:
                try:
                    async for _ in graph.astream(
                        input_state,
                        config,
                        stream_mode=["values"],
                    ):
                        # 消费状态流仅用于推进图；canonical conversation state 的变更
                        # 已由节点通过 RuntimeOperations 提交，不能从 graph stream 反推事实。
                        continue
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
                interrupts = list(tasks[0].interrupts)
                if not interrupts:
                    break

                # ★ 取消检查：graph 暂停在 interrupt()（等待审批），若 run 已取消则不恢复
                if operations.is_current_run_cancelled():
                    log.info(
                        "workflow_interrupt_cancelled",
                        extra={
                            "msg": f"interrupt 暂停时 run 已取消，不再恢复，run_id={run_id}",
                            "data": {"run_id": run_id},
                        },
                    )
                    break

                # 此分支仅在「存在 approval_resolver」时进入：无审批器时 tools 节点不会
                # 调用 interrupt()，graph 不会暂停，外层循环已在上面 `not interrupts` 处退出。
                interrupt_value = interrupts[0].value
                pending = (
                    interrupt_value.get(APPROVAL_INTERRUPT_KEY, [])
                    if isinstance(interrupt_value, dict)
                    else []
                )
                approval_resolver = runtime_config.approval_resolver
                approved = approval_resolver(pending) if approval_resolver is not None else pending
                input_state = Command(resume=approved)
