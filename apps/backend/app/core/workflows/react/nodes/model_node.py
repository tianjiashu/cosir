"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量写入 workflow custom
stream；用 ``model.astream()`` 消费流式输出（草稿由 ``RuntimeContextManager`` 累积并收口成完整
``AIMessage``），根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大
步数终止。run 状态变更经 ``WorkflowOperations`` 落到 ``ConversationRun``（唯一事实源）。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_content`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支），但模型这段说明并非丢弃——
它会经 canonical conversation facts 写入、经 ``RuntimeContextManager.add_message`` /
``add_message_chunk``
落库进历史上下文，并随 state ``instruction`` 字段下传给 ``tools`` / ``observe`` 节点，
使下游执行与错误排查能看到模型当时的意图。

模型侧数据处理辅助（流式 chunk 解析 ``ModelChunkProcessor``、流式 part 生命周期）
已拆为独立模块，本模块仅 import 使用；节点共享运行时原语见 ``common``。
"""

import asyncio
from typing import Deque

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage, BaseMessage
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.config.logging.logger import log
from app.core.tools.schemas import ToolCall
from app.core.workflows.react.nodes.helper.common import (
    _runtime_config,
    _runtime_context,
    terminal_state,
)
from app.core.workflows.react.nodes.helper.finalize_max_steps import _finalize_max_steps
from app.core.workflows.react.nodes.helper.model_chunk import ModelChunkProcessor
from app.core.workflows.react.nodes.helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.core.workflows.react.nodes.helper.tool_call_lifecycle import ToolCallLifecycleManager
from app.core.workflows.vision_input import resolve_messages_for_model
from app.utils.message_content import content_to_text

from app.core.workflows.react.state import ReactGraphState

# ``finish_reason`` 是 Provider 语义，不直接等同于工作流终态。不同兼容层可能使用
# ``stop``、``end`` 或 ``end_turn`` 表示正常文本结束；在 model 节点内做最小归一化，避免
# 把 provider-specific 字符串扩散到 graph edge 与终态写入逻辑。
_NORMAL_FINISH_REASONS = frozenset({"stop", "end", "end_turn"})
_CONTINUATION_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

system_queue: Deque[SystemMessage] = Deque()


def _build_continuation_prompt(finish_reason: str | None) -> str:
    """为未完成的模型输出构造一次模型可消费的继续提示。

    ``length`` 类原因明确表示达到输出上限；缺失或未知原因则表示 Provider/适配器没有
    提供可确认的正常结束信号。两类情况都不应把已有文本直接标记为最终回答，提示内容
    要求模型从已有输出之后继续，避免重复已完成部分。

    参数:
        finish_reason: 已归一化的 Provider 完成原因，可为 ``None``。

    返回:
        追加到 canonical context 的 ``SystemMessage`` 文本。

    异常:
        无。

    副作用:
        无。
    """

    if finish_reason in _CONTINUATION_FINISH_REASONS:
        return (
            "Your previous response was truncated by the output length limit. "
            "Continue from where it stopped, do not repeat completed content, and finish the "
            "answer. If a tool is required, issue the tool call instead of describing it."
        )
    reason_text = finish_reason or "missing"
    return (
        "Your previous response did not provide a recognized completion signal "
        f"(finish_reason={reason_text}). Treat it as incomplete, continue from where it stopped, "
        "do not repeat completed content, and finish the answer."
    )


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量
    写入 workflow stream；用 ``model.astream()`` 消费流式输出（草稿由 ``RuntimeContextManager``
    累积并收口成完整 ``AIMessage``）。根据模型最终输出决定进入工具分支、最终回答分支，还是因
    无效输出 / 超过最大步数而终止。run 状态变更经 ``WorkflowOperations`` 落到
    ``ConversationRun``（唯一事实源）。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）；当本次推理
        已超配额（``step_count > max_steps``）时不发起推理，直接返回
        ``_finalize_max_steps`` 的终态 patch_write。

    副作用:
        - 发起推理前若本次已超配额，调用 ``_finalize_max_steps`` 收口终态，由 canonical
          writer 把 run 标记为 ``max_steps_reached`` 失败，不再触发推理；
        - 经 ``RuntimeContextManager.add_message_chunk`` 增量持久化本轮 assistant 草稿，
          正常结束后由 ``flush_message_chunk(complete=True)`` 收口为完整 ``AIMessage``，
          使下一模型步能累积看到本轮输出；
        - 模型文本与 reasoning 增量经 LangGraph custom stream 写给 workflow；由 workflow
          统一调用 ``RuntimeOperations`` 更新 snapshot；状态写入 ``run``；
        - 非法输出经 ``RuntimeOperations`` 落定失败；请求前 / 流式中 / 流式结束后检测到协作
          取消时，经 ``RuntimeOperations.cancel_run_if_running`` 落定取消终态并 ``interrupt``
          挂起本节点（不写路由标志、不结束图，该 run 仍可由续跑恢复）；
        - ``invalid_tool_calls`` 的判定已下沉到 ``ToolCallLifecycleManager.classify``：按 ``id``
          对齐模型未解析成功的调用，命中者挂 ``invalid_detail`` 并维持 ``pending``，由 observe
          节点统一结算并注入修复 ``SystemMessage``（排在全部 ToolMessage 之后，避免产生
          ``AIMessage(tool_calls) -> SystemMessage -> ToolMessage`` 的非法顺序）；缺失 ``id``
          的非法调用无法对齐，仅记 warning。
        - 模型没有工具调用时，只有 Provider 明确报告正常完成原因才标记最终回答；长度截断、
          缺失或未知完成原因会追加继续提示并通过 ``continue_model`` 回到模型节点。

    异常:
        RuntimeError: 超步数收口时 ``RuntimeConfig`` 未携带 run id（见 ``_finalize_max_steps``）。
        Exception: 模型调用或流式消费失败时向上传播，由 runner 收敛 run 终态。
    """

    rc = _runtime_config()
    operations = rc.operations
    model = rc.model
    thinking_channel = rc.thinking_channel
    chunk_processor = ModelChunkProcessor(thinking_channel)
    stream_writer = get_stream_writer()

    task_id = operations.get_current_run().task_id
    run_id = operations.get_current_run().id

    step_count = state.step_count + 1
    # 提前拦截：本次推理若已超配额（step_count > max_steps）
    if step_count > state.max_steps:
        return await _finalize_max_steps(state, step_count=step_count)
    step_id = f"step-{step_count}"
    if operations.is_current_run_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 run 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": rc.run.id},
            },
        )
        operations.cancel_run_if_running(usage_stats=rc.usage_stats, final_output="user_cancelled")
        interrupt({"reason": "user_cancelled"})
    # load_message() 出口已归一化 assistant 消息，此处直接取用，不再重复 sanitize。
    messages: list[BaseMessage] = _runtime_context().load_message()

    while len(system_queue) > 0:
        system_message = system_queue.pop()
        _runtime_context().add_message(system_message)
        messages.append(system_message)

    messages = await asyncio.to_thread(
        resolve_messages_for_model,
        messages,
        task_id=task_id,
        model_name=getattr(rc.run, "model_name", None) or "",
        vision_input_format=getattr(rc, "vision_input_format", "openai_url"),
    )

    log.info(
        "model_node_started",
        extra={
            "msg": f"模型节点开始执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": step_count,
                "message_count": len(messages),
            },
        },
    )
    # chunks 攒结构化分块合并成 AIMessage 供解析 tool_calls 与提取最终正文。
    chunks: list[AIMessageChunk] = []

    log.info(
        "model_node_model_requested",
        extra={
            "msg": f"模型节点请求模型，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "run_id": rc.run.id,
                "message_count": len(messages),
            },
        },
    )
    # part 生命周期收口：把交错的 text/reasoning 流转换为顺序括号化的 part 事件流。
    parts = StreamingPartStateMachine(
        stream_writer, task_id=task_id, run_id=run_id, step_id=step_id
    )

    # lifecycle 只覆盖本次 model request；model -> tools -> observe 之间会沿 state 传递，
    # 下一次进入 model 时从空快照开始，避免混入上一轮已结束的 tool call。
    state.tool_call_lifecycle = ToolCallLifecycleManager()
    lifecycle = state.tool_call_lifecycle
    ai_message: AIMessage | None = None

    async for chunk in model.astream(messages):
        chunks.append(chunk)
        message_chunk = _runtime_context().add_message_chunk(
            chunk, stream_id=step_id, run_id=run_id
        )

        if operations.is_current_run_cancelled():
            log.warning(
                "model_node_cancelled_usage_summary",
                extra={
                    "msg": f"模型流式因取消提前终止，本轮已消耗 token 摘要，step_id={step_id}",
                    "data": {"step_id": step_id, "usage": rc.usage_stats.to_dict()},
                },
            )
            ai_message = _runtime_context().flush_message_chunk(
                stream_id=step_id, run_id=run_id, mode="cancel"
            )
            parts.finish()
            break

        # 提取文本与 reasoning 内容。
        text = content_to_text(chunk.content)
        # 提取 reasoning 内容。
        reasoning = chunk_processor.extract_reasoning(chunk)
        # 模型把 "\n"、" \n" 单独作为 chunk 时，也要保留
        if text:
            parts.text(text)
        if reasoning and reasoning.strip():
            parts.reasoning(reasoning)
        # 传入的是本步累积后的 AIMessage（``add_message_chunk`` 的返回），而非原始
        # chunk：``extract_tool_calls`` 按属性读取 ``tool_call_chunks`` / ``tool_calls``，
        # 两种形态都适用（聚合后调用通常已落在 ``tool_calls``）。
        raw_tool_calls = chunk_processor.extract_tool_calls(message_chunk)
        if raw_tool_calls:
            # 一个 chunk 可能并行携带多个 tool call，逐条处理已有的 name/id 身份。
            parts.tool_call()
            lifecycle = lifecycle.create(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                raw_tool_calls=raw_tool_calls,
            )

    if not operations.is_current_run_cancelled():
        parts.finish()
        ai_message: AIMessage = _runtime_context().flush_message_chunk(
            stream_id=step_id, run_id=run_id, mode="complete"
        )

    if ai_message is None:
        operations.cancel_run_if_running(usage_stats=rc.usage_stats, final_output="user_cancelled")
        interrupt({"reason": "user_cancelled"})

    finish_reason = chunk_processor.extract_finish_reason(ai_message)

    # 累加 usage_metadata 到 run 级共享累加器。
    rc.usage_stats.add_usage_metadata(getattr(ai_message, "usage_metadata", None))

    # 把模型输出拆解为工具调用生命周期：合法调用置 running，可修复非法调用挂 invalid_detail
    # （由 observe 节点统一结算并构造修复提示），未命中工具名的噪声仅记 warning。invalid
    # 判定从 model_node 下沉到 ToolCallLifecycleManager，本节点不再分支处理，职责收敛为
    # 「消费模型输出、决定工具/最终回答/非法输出」三类走向。
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    tool_calls = [ToolCall.from_from_langchain(call) for call in ai_message.tool_calls]
    lifecycle = lifecycle.classify(
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
    )

    repair_message = lifecycle.fail_invalid_tools(task_id=task_id, run_id=run_id, step_id=step_id)
    # 3. 注入修复提示（若有可修复非法调用）：必须排在全部 ToolMessage 之后,通过system_queue延后注入.
    if repair_message:
        system_queue.append(SystemMessage(content=repair_message))

    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(tool_calls),
                "invalid_count": lifecycle.invalid_count,
                "output_text_length": len(ai_message.content),
                "finish_reason": finish_reason,
            },
        },
    )

    if lifecycle.has_call:
        # 有任意工具调用（合法或非法）都进 tools 节点：合法调用执行，非法调用由 observe 结算。
        # 不再区分 requested_tool / repair_requested —— observe 非终态即经 _after_observe
        # 回流 model。
        return {
            "step_count": step_count,
            "requested_tool": True,
            "continue_model": False,
            "final_response": False,
            "terminal": False,
            "instruction": ai_message.content if isinstance(ai_message.content, str) else "",
            "tool_call_lifecycle": lifecycle,
        }

    if operations.is_current_run_cancelled():
        operations.cancel_run_if_running(usage_stats=rc.usage_stats, final_output="user_cancelled")
        interrupt({"reason": "user_cancelled"})

    if finish_reason in _NORMAL_FINISH_REASONS and ai_message.content:
        # 没有工具调用且 Provider 明确报告正常结束 → 最终回答。
        final_answer = ai_message.content if isinstance(ai_message.content, str) else None
        completed_run = operations.complete_run_if_running(
            rc.usage_stats, final_output=final_answer
        )
        if completed_run is None:
            log.info(
                "model_node_final_response_terminal_race_lost",
                extra={
                    "msg": f"最终回复落定时 run 已非 running，跳过完成事件，step_id={step_id}",
                    "data": {"step_id": step_id, "run_id": rc.run.id},
                },
            )
            return terminal_state(step_count, requested_tool=False)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
            },
        )
        return {
            **terminal_state(step_count, final_response=True, requested_tool=False),
            "final_text": ai_message.content,
        }

    if finish_reason not in _NORMAL_FINISH_REASONS:
        # 已有文本不代表模型完成：例如 finish_reason=length 只说明本轮达到输出上限。
        # AIMessage 已先落库，SystemMessage 紧跟其后作为下一模型步的显式续写指令。
        continuation_prompt = _build_continuation_prompt(finish_reason)
        _runtime_context().add_message(SystemMessage(content=continuation_prompt))
        log.warning(
            "model_node_output_requires_continuation",
            extra={
                "msg": "模型输出没有可接受的正常完成原因，追加继续提示并回到模型节点",
                "data": {
                    "step_id": step_id,
                    "run_id": rc.run.id,
                    "finish_reason": finish_reason,
                    "output_text_length": len(ai_message.content),
                },
            },
        )
        return {
            "step_count": step_count,
            "requested_tool": False,
            "continue_model": True,
            "final_response": False,
            "terminal": False,
            "instruction": "",
        }

    #如果存在系统修复提示，重新进入model
    if len(system_queue) > 0:
        return {
            "step_count": step_count,
            "requested_tool": False,
            "continue_model": True,
            "final_response": False,
            "terminal": False,
            "instruction": "",
        }

    log.warning(
        "model_node_invalid_output",
        extra={
            "msg": f"模型既未返回工具调用也无有效文本，判定为非法输出，step_id={step_id}",
            "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
        },
    )
    failed_run = operations.fail_run_if_running(
        end_reason="invalid_model_output",
        usage_stats=rc.usage_stats,
        final_output="模型既未返回工具调用也无有效文本，判定为非法输出",
    )
    if failed_run is None:
        log.info(
            "model_node_invalid_output_terminal_race_lost",
            extra={
                "msg": f"非法模型输出失败落定时 run 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": rc.run.id},
            },
        )
    return terminal_state(step_count, requested_tool=False)
