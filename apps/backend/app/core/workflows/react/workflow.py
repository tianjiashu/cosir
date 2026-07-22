"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译 graph（``model`` / ``tools`` 节点 +
``should_continue`` 条件边）、以 ``astream(stream_mode=["custom","messages"])`` 单循环
驱动执行，把节点经 ``get_stream_writer()`` 写入的 ``custom`` 业务事件与 ``messages``
流中的 token 分块统一翻译为 ``RuntimeEvent`` 对外流式 ``yield``；graph 编译时挂
``AsyncSqliteSaver`` checkpointer，由 LangGraph 负责状态持久化、断点续跑与审批中断。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..agent_workflow import AgentWorkflow
from ...runtime.runtime_operations import RuntimeOperations
logger = logging.getLogger(__name__)

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.core.llm.factory import build_chat_model
from app.core.llm.langchain_bridge import model_tools_to_langchain, runtime_to_langchain
from app.core.runtime.runs.checkpointer import build_checkpointer
from app.models import TaskRecord
from app.models.enums.event_type import EventType
from app.models.runtime_event import RuntimeEvent
from app.tools.schemas import ToolCall

from .edges import _should_continue
from .nodes import _extract_text, _model_node, _tools_node
from .runtime_config import RuntimeConfig
from .state import ReactGraphState


class ReactLikeWorkflow(AgentWorkflow):
    """基于“模型推理 -> 工具调用 -> 继续推理/最终回答”的默认工作流，由 LangGraph 编排。

    该类只承担执行策略职责，不直接创建模型、工具或数据库连接。所有外部能力都通过
    ``RuntimeOperations`` 注入；graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph
    负责状态持久化、断点续跑与 ``interrupt()`` 审批中断。
    """

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
    ) -> AsyncIterator[RuntimeEvent]:
        """执行一个任务，直到完成、失败、取消或达到最大步骤数。

        方法构建并编译 graph，挂 ``AsyncSqliteSaver`` checkpointer；以
        ``astream(stream_mode=["custom","messages"])`` 单循环驱动 graph，把节点经
        ``get_stream_writer()`` 写入的 ``custom`` 业务事件与 ``messages`` 流中的 token 分块
        统一翻译为 ``RuntimeEvent`` 流式 ``yield``。当 ``tools`` 节点触发 ``interrupt()`` 时，
        方法用审批解析器解析出批准的工具调用，并通过 ``Command(resume=)`` 恢复 graph，直到
        工作流结束。

        Args:
            task: 当前需要执行的任务记录。
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新能力。

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
        tool_schemas = model_tools_to_langchain(operations.model_tools, agent_profile.allowed_tools)
        try:
            bound_model = base_model.bind_tools(tool_schemas) if tool_schemas else base_model
        except NotImplementedError:
            logger.warning(
                "model %s does not support bind_tools; running without tools "
                "(expected when no real API key is configured)",
                type(base_model).__name__,
            )
            bound_model = base_model

        config = {
            "configurable": {
                "thread_id": thread_id,
                "runtime_config": RuntimeConfig(
                    operations=operations,
                    task=task,
                    turn=turn,
                    model=bound_model,
                    approval_resolver=self._approval_resolver,
                ),
            }
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

            current_step_id: str | None = None
            sequence = 0
            while True:
                try:
                    async for mode, data in graph.astream(
                            input_state,
                            config,
                            stream_mode=["custom", "messages"],
                    ):
                        if mode == "messages":
                            chunk, _metadata = data
                            text = _extract_text(chunk.content)
                            if text and current_step_id is not None:
                                yield RuntimeEvent(
                                    event_type=EventType.MODEL_OUTPUT_DELTA,
                                    task_id=task.task_id,
                                    turn_id=turn_id,
                                    sequence=sequence,
                                    payload={"step_id": current_step_id, "text": text},
                                )
                                sequence += 1
                        elif mode == "custom":
                            raw = data
                            event_type = EventType(raw["event_type"])
                            payload = raw.get("payload", {})
                            if event_type == EventType.STEP_STARTED:
                                current_step_id = payload.get("step_id")
                            yield RuntimeEvent(
                                event_type=event_type,
                                task_id=task.task_id,
                                turn_id=turn_id,
                                sequence=sequence,
                                payload=payload,
                            )
                            sequence += 1
                except Exception:
                    operations.log_exception(
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
                interrupt_value = interrupts[0].value
                pending = (
                    interrupt_value.get("tool_calls", [])
                    if isinstance(interrupt_value, dict)
                    else []
                )
                resolver = config["configurable"]["runtime_config"].approval_resolver
                approved = resolver(pending) if resolver is not None else pending
                input_state = Command(resume=approved)
