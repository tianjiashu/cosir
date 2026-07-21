"""ReAct-like 工作流的 LangGraph 节点行为。

本模块只承载节点逻辑，不负责 graph 构建、运行编排或事件翻译。两个节点 ``model`` 与
``tools`` 均为 LangGraph 原生 callable，通过 ``get_config()`` 从运行上下文取出
``operations`` / ``task`` / ``turn`` / ``model``；节点业务事件通过 ``get_stream_writer()`` 写入
（在 ``astream(stream_mode=["custom","messages"])`` 中表现为 ``custom`` 事件），token 由
``model.astream()`` 产出、由编排层从 ``messages`` 流中捕获，二者同走一条原生事件流。

状态单一事实来源是 ``Turn``：节点经 ``operations`` 写 **turn** 状态，不再写 task 执行态。
"""

from dataclasses import asdict

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.config import get_config, get_stream_writer
from langgraph.types import interrupt

from app.core.llm.langchain_bridge import runtime_to_langchain, tool_calls_from_langchain
from app.models.enums.event_type import EventType
from app.tools.schemas import ToolCall

from .state import ReactGraphState


def _extract_text(content) -> str:
    """从 LangChain 消息 content 中提取纯文本分片。

    参数:
        content: LangChain 消息的 ``content`` 字段（字符串或分块列表）。

    返回:
        拼接后的纯文本；无法识别时返回空字符串。
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _finalize_ai_message(chunks: list[AIMessageChunk]) -> AIMessage:
    """把累积的 ``AIMessageChunk`` 列表合并为标准的 ``AIMessage``。

    参数:
        chunks: 模型流式产出的分块列表（可能为空）。

    返回:
        可安全存入 graph state 并交给下一步模型调用的 ``AIMessage``。
    """

    merged: AIMessageChunk | None = None
    for chunk in chunks:
        merged = chunk if merged is None else merged + chunk
    if merged is None:
        return AIMessage(content="")
    return AIMessage(
        content=merged.content,
        tool_calls=merged.tool_calls or [],
        id=getattr(merged, "id", None),
    )


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``task`` / ``turn`` / ``model``，通过
    ``get_stream_writer()`` 把业务生命周期事件写入自定义事件流；用 ``model.astream()`` 累积
    ``AIMessage``，token 增量由编排层从 ``messages`` 流捕获。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **turn**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）。
    """

    cfg = get_config()["configurable"]
    operations = cfg["operations"]
    turn = cfg["turn"]
    model = cfg["model"]
    writer = get_stream_writer()

    def write_event(event_type: EventType, payload: dict) -> None:
        writer({"event_type": str(event_type), "payload": dict(payload)})

    step_count = state.step_count + 1
    step_id = f"step-{step_count}"
    write_event(EventType.STEP_STARTED, {"step_id": step_id, "kind": "model", "index": step_count})
    write_event(
        EventType.MODEL_REQUESTED, {"step_id": step_id, "message_count": len(state.messages)}
    )

    collected_text: list[str] = []
    chunks: list[AIMessageChunk] = []
    terminal = False

    async for chunk in model.astream(state.messages):
        if operations.has_turn_status(turn.turn_id, "cancelled"):
            write_event(
                EventType.RUN_CANCELLED,
                {"step_id": step_id, "status": "cancelled", "error": "cancelled"},
            )
            terminal = True
            break
        text = _extract_text(chunk.content)
        if text:
            collected_text.append(text)
        chunks.append(chunk)

    if terminal:
        operations.update_turn_status(turn.turn_id, "cancelled")
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "messages": [],
            "pending_tool_calls": [],
        }

    ai_message = _finalize_ai_message(chunks)
    tool_calls: list[ToolCall] = tool_calls_from_langchain(ai_message.tool_calls or [])
    output_text = "".join(collected_text).strip()
    requested_tool = bool(tool_calls)

    write_event(
        EventType.MODEL_COMPLETED,
        {
            "step_id": step_id,
            "text": output_text,
            "tool_calls": [asdict(call) for call in tool_calls],
        },
    )

    if requested_tool:
        if step_count >= state.max_steps:
            write_event(EventType.RUN_FAILED, {"status": "failed", "error": "max_steps_reached"})
            operations.update_turn_status(turn.turn_id, "failed")
            return {
                "step_count": step_count,
                "requested_tool": False,
                "final_response": False,
                "terminal": True,
                "messages": [ai_message],
                "pending_tool_calls": [],
            }
        return {
            "step_count": step_count,
            "requested_tool": True,
            "final_response": False,
            "terminal": False,
            "messages": [ai_message],
            "pending_tool_calls": [asdict(call) for call in tool_calls],
        }

    if output_text:
        write_event(
            EventType.FINAL_RESPONSE,
            {"text": output_text, "step_id": step_id, "status": "completed"},
        )
        operations.update_turn_status(turn.turn_id, "completed")
        operations.update_turn_response(turn.turn_id, output_text)
        write_event(EventType.RUN_FINISHED, {"status": "completed", "step_id": step_id})
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": True,
            "terminal": True,
            "messages": [ai_message],
            "pending_tool_calls": [],
            "final_text": output_text,
        }

    write_event(
        EventType.RUN_FAILED,
        {
            "error": "invalid_model_output",
            "message": "Model did not return tool call or final text.",
        },
    )
    operations.update_turn_status(turn.turn_id, "failed")
    return {
        "step_count": step_count,
        "requested_tool": False,
        "final_response": False,
        "terminal": True,
        "messages": [ai_message],
        "pending_tool_calls": [],
    }


def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：在权限审批后执行工具并把观察结果追加回上下文。

    节点先用 ``interrupt()`` 暂停 graph 等待审批，审批结果（批准的工具调用列表）通过
    ``Command(resume=)`` 恢复；随后通过 ``RuntimeOperations`` 执行工具，工具生命周期事件
    经 ``write_event`` 回调写入自定义事件流；观察消息由 bridge 转为 ``BaseMessage`` 存回 state。
    状态写入 **turn**。

    参数:
        state: 当前 graph state，含待执行工具调用。

    返回:
        需要合并回 graph state 的增量（工具错误计数、新增观察消息等）。
    """

    cfg = get_config()["configurable"]
    operations = cfg["operations"]
    task = cfg["task"]
    turn = cfg["turn"]
    writer = get_stream_writer()

    def write_event(event_type: EventType, payload: dict) -> None:
        writer({"event_type": str(event_type), "payload": dict(payload)})

    tool_calls = state.pending_tool_calls
    step_id = f"step-{state.step_count}"

    approved = interrupt({"tool_calls": tool_calls})
    approved_dicts = tool_calls if not isinstance(approved, list) else approved
    approved_calls = [
        ToolCall(
            tool_name=item["tool_name"],
            arguments=item.get("arguments") or {},
            call_id=item.get("call_id") or "",
        )
        for item in approved_dicts
    ]

    tool_run = operations.run_tool_calls(
        task.task_id,
        approved_calls,
        step_id,
        write_event=write_event,
    )
    observations = tool_run.observations

    tool_error_count = state.tool_error_count
    for observation in observations:
        if observation.status == "success":
            tool_error_count = 0
        else:
            tool_error_count += 1

    if tool_error_count >= operations.settings.tool_error_limit:
        write_event(
            EventType.RUN_FAILED,
            {
                "step_id": step_id,
                "status": "failed",
                "error": "tool_error_limit_reached",
                "tool_name": observations[0].tool_name if observations else "",
            },
        )
        operations.update_turn_status(turn.turn_id, "failed")
        return {
            "pending_tool_calls": [],
            "tool_error_count": tool_error_count,
            "terminal": True,
            "messages": [],
        }

    return {
        "pending_tool_calls": [],
        "tool_error_count": tool_error_count,
        "messages": runtime_to_langchain(tool_run.messages_for_model),
    }
