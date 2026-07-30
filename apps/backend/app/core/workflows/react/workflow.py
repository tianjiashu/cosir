"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译 graph（``model`` / ``tools`` 节点 +
``should_continue`` 条件边）、以 ``astream(stream_mode=["custom"])`` 单循环
驱动执行，把节点经 ``get_stream_writer()`` 写入的 ``custom`` 业务事件（含模型回复增量与思考增量）
统一透传为 ``RuntimeEvent`` 对外流式 ``yield``；graph 编译时挂
``AsyncSqliteSaver`` checkpointer，由 LangGraph 负责状态持久化、断点续跑与审批中断。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config.logging.logger import log
from app.core.llm.factory import build_chat_model
from app.core.llm.langchain_bridge import model_tools_to_langchain, runtime_to_langchain
from app.core.runtime.runs.checkpointer import build_checkpointer
from app.models import TaskRecord
from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.models.runtime_event import RuntimeEvent
from app.tools.schemas import ToolCall

from ...runtime.runtime_operations import RuntimeOperations
from ..agent_workflow import AgentWorkflow
from .edges import _should_continue
from .nodes import _model_node, _tools_node
from .runtime_config import RuntimeConfig
from .state import ReactGraphState


class ReactLikeWorkflow(AgentWorkflow):
    """基于“模型推理 -> 工具调用 -> 继续推理/最终回答”的默认工作流，由 LangGraph 编排。

    该类只承担执行策略职责，不直接创建模型、工具或数据库连接。所有外部能力都通过
    ``RuntimeOperations`` 注入；graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph
    负责状态持久化、断点续跑与 ``interrupt()`` 审批中断。
    """

    workflow_id = "react_like_v1"

    def __init__(
        self,
        approval_resolver: Callable[[list[ToolCall]], list[ToolCall]] | None = None,
    ) -> None:
        """初始化 ReAct-like 工作流。

        Args:
            approval_resolver: 可选的工具审批解析器。当 ``tools`` 节点因 ``interrupt()`` 暂停时，
                工作流会用它把待审批的工具调用解析为「批准执行的调用列表」并通过
                ``Command(resume=)`` 恢复 graph。缺省为 ``None``，表示直接批准全部调用。
        """

        self._approval_resolver = approval_resolver

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        节点 ``model`` 与 ``tools`` 通过 ``should_continue`` 条件边形成 ReAct 循环；graph 编译时
        挂入 ``checkpointer`` 以启用 checkpoint 持久化与 ``interrupt`` 恢复。

        参数:
            checkpointer: 已配置好的 LangGraph checkpointer。

        返回:
            已编译的 StateGraph。
        """

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _model_node)
        builder.add_node("tools", _tools_node)
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", _should_continue, {"tools": "tools", END: END})
        builder.add_edge("tools", "model")
        return builder.compile(checkpointer=checkpointer)

    async def run(
        self,
        task: TaskRecord,
        operations: RuntimeOperations,
        callbacks: list | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """执行一个任务，直到完成、失败、取消或达到最大步骤数。

        方法构建并编译 graph，挂 ``AsyncSqliteSaver`` checkpointer；以
        ``astream(stream_mode=["custom"])`` 单循环驱动 graph，把节点经 ``get_stream_writer()``
        写入的 ``custom`` 业务事件（含 ``MODEL_OUTPUT_DELTA`` / ``MODEL_THINKING_DELTA`` 等流式
        增量）统一透传为 ``RuntimeEvent`` 流式 ``yield``。当 ``tools`` 节点触发 ``interrupt()`` 时，
        方法用审批解析器解析出批准的工具调用，并通过 ``Command(resume=)`` 恢复 graph，直到
        工作流结束。

        Args:
            task: 当前需要执行的任务记录。
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新能力。
            callbacks: 可选的 LangChain callbacks（如 Langfuse ``CallbackHandler``），
                注入 ``graph.astream`` 的 ``config["callbacks"]``，使 LLM 调用被自动追踪；
                缺省为空列表，不影响既有行为。

        Yields:
            RuntimeEvent: 任务执行过程中产生的运行时事件，供 API 层继续转换为 SSE 或其他客户端事件。
        """

        turn = operations.get_current_turn()
        thread_id = turn.turn_id
        turn_id = turn.turn_id

        # Agent 执行主体
        agent_profile = operations.agent_profile
        # 构建模型
        base_model = build_chat_model(
            agent_profile.model_name,
            model_settings=agent_profile.model_settings,
        )
        # 构建工具
        tool_schemas = model_tools_to_langchain(
            operations.model_tools, set(agent_profile.allowed_tools)
        )
        try:
            bound_model = base_model.bind_tools(tool_schemas) if tool_schemas else base_model
        except NotImplementedError:
            log.warning(
                "model %s does not support bind_tools; running without tools "
                "(expected when no real API key is configured)",
                type(base_model).__name__,
            )
            bound_model = base_model

        runtime_config = RuntimeConfig(
            operations=operations,
            task=task,
            turn=turn,
            model=cast(BaseChatModel, bound_model),
            approval_resolver=self._approval_resolver,
        )
        config = {
            "configurable": {
                "thread_id": thread_id,
                "runtime_config": runtime_config,
            },
            # LangChain callbacks（如 Langfuse CallbackHandler）经此注入模型调用追踪；
            # 缺省空列表不影响既有行为。
            "callbacks": callbacks or [],
        }

        async with build_checkpointer() as checkpointer:
            graph = self._build_graph(checkpointer)
            runtime_messages = operations.build_messages()
            input_state: ReactGraphState | Command = ReactGraphState(
                messages=runtime_to_langchain(runtime_messages),
                step_count=0,
                tool_error_count=0,
                requested_tool=False,
                final_response=False,
                terminal=False,
                pending_tool_calls=[],
                max_steps=agent_profile.max_steps,
                final_text="",
            )

            sequence = 0
            while True:
                try:
                    async for mode, data in graph.astream(
                        input_state,
                        config,
                        stream_mode=["custom"],
                    ):
                        if mode != "custom":
                            continue  # 仅消费 custom 事件流（回复/思考增量均来自节点内）
                        raw = data
                        event_type = EventType(raw["event_type"])
                        payload = raw["payload"]
                        if not isinstance(payload, RuntimeEventPayload):
                            raise TypeError("custom runtime event payload must be a payload entity")
                        event = RuntimeEvent(
                            event_type=event_type,
                            task_id=task.task_id,
                            turn_id=turn_id,
                            sequence=sequence,
                            payload=payload,
                        )
                        log.info(
                            "workflow_graph_event",
                            extra={"msg": "workflow graph event", "data": event.to_dict()},
                        )
                        yield event
                        sequence += 1
                except Exception:
                    log.exception(
                        "workflow_graph_failed",
                        extra={
                            "msg": "langgraph execution failed during workflow run",
                            "data": {
                                "task_id": task.task_id,
                                "turn_id": getattr(operations, "_current_turn_id", None),
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

                # ★ 取消检查：graph 暂停在 interrupt()（等待审批），若 turn 已取消则不恢复
                if operations.has_turn_status(turn_id, "cancelled"):
                    log.info(
                        "workflow_interrupt_cancelled",
                        extra={
                            "msg": f"interrupt 暂停时 turn 已取消，不再恢复，turn_id={turn_id}",
                            "data": {"turn_id": turn_id},
                        },
                    )
                    yield RuntimeEvent(
                        event_type=EventType.RUN_CANCELLED,
                        task_id=task.task_id,
                        turn_id=turn_id,
                        sequence=sequence,
                        payload=RunCancelledPayload(status="cancelled"),
                    )
                    sequence += 1
                    break

                interrupt_value = interrupts[0].value
                pending = (
                    interrupt_value.get("tool_calls", [])
                    if isinstance(interrupt_value, dict)
                    else []
                )
                resolver = runtime_config.approval_resolver
                approved = resolver(pending) if resolver is not None else pending
                input_state = Command(resume=approved)
