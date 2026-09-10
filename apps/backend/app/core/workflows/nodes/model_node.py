"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量写入 workflow custom
stream；用 ``model.astream()`` 累积 ``AIMessage``，
根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大步数终止。
状态写入 **run**。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_files`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支），但模型这段说明并非丢弃——
它会经 canonical conversation facts 写入、经 ``RuntimeContextManager.add_message``
落库进历史上下文，并在进入工具分支时作为 ``instruction`` 键随 ``pending_tool_calls``
下传给 ``tools`` / ``observe`` 节点，使下游执行与错误排查能看到模型当时的意图。

模型侧数据处理辅助（思考通道抽取、chunk 组装、chunk debug 落盘、流式 part 生命周期）已拆为
独立模块（``thinking_extractor`` / ``chunk_assembler`` / ``debug_dump`` /
``streaming_part_state_machine``），本模块仅 import 使用；节点共享运行时原语见 ``common``。
"""

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage
from langgraph.config import get_stream_writer

from app.assistant_transport.event import (
    ToolCallCreatedEvent,
    ToolCallStatusChangedEvent,
    UsageUpdatedEvent,
)
from app.config.logging.logger import log
from app.core.tools.schemas import ToolCall
from app.core.workflows.nodes.helper.chunk_assembler import (
    _collect_chunk_to_ai_message,
    _has_content,
)
from app.core.workflows.nodes.helper.common import (
    _runtime_config,
    _runtime_context,
    terminal_state,
)
from app.core.workflows.nodes.helper.debug_dump import (
    # 测试经 model_node._dump_merged_chunk_debug 访问
    _dump_raw_chunk_debug,
)
from app.core.workflows.nodes.helper.finalize_max_steps import _finalize_max_steps
from app.core.workflows.nodes.helper.invalid_tool_call import (
    TOOL_CALL_REPAIR_MESSAGE_KIND,
    InvalidToolOutcome,
    build_invalid_tool_call_repair_message,
    decide_invalid_tool_handling,
)
from app.core.workflows.nodes.helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.core.workflows.nodes.helper.thinking_extractor import (
    extract_reasoning_content,
    # 测试经 model_node._should_strip_reasoning_content 访问
)
from app.core.workflows.nodes.helper.tool_name_extractor import extract_tool_calls
from app.utils.message_content import content_to_text

from ..react.state import ReactGraphState


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量
    写入 workflow stream；用 ``model.astream()`` 累积
    ``AIMessage``。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **run**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）；当本次推理
        已超配额（``step_count > max_steps``）时不发起推理，直接返回
        ``_finalize_max_steps`` 的终态 patch。

    副作用:
        - 发起推理前若本次已超配额，直接调用 ``_finalize_max_steps`` 收口终态（发
          把 run 标记为 failed），不再触发推理；
        - 经 ``_runtime_context().add_message`` 把本轮 ``AIMessage`` 落库并写回内存
          （``RuntimeContextManager`` 唯一写入入口），使下一模型步能累积看到本轮输出；
        - 模型文本与 reasoning 增量经 LangGraph custom stream 写给 workflow；由 workflow
          统一调用 ``RuntimeOperations`` 更新 snapshot；状态写入 ``run``；
        - 非法输出经 ``RuntimeOperations`` 落定失败；请求前/流式中取消经同一门面落定取消；
        - ``invalid_tool_calls`` 按双轨消费：未命中工具名的 ``IGNORE`` 仅记 warning；
          命中工具名的 ``REPAIR`` 在无合法调用时于当前 AIMessage 之后立即注入并回流
          model，在存在合法调用时只写入 ``deferred_repair_message``，由 observe 在全部
          ToolMessage 之后追加，避免产生 ``AIMessage(tool_calls) -> SystemMessage ->
          ToolMessage`` 的非法顺序。
    """

    rc = _runtime_config()
    operations = rc.operations
    model = rc.model
    thinking_channel = rc.thinking_channel
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
        # 请求前取消同样走统一 canonical 终态，与流式中取消/工具取消保持语义一致。
        operations.cancel_run_if_running(end_reason="runtime_cancelled", usage_stats=rc.usage_stats)
        return terminal_state(step_count)
    # load_message() 出口已归一化 assistant 消息，此处直接取用，不再重复 sanitize。
    messages = _runtime_context().load_message()
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
    chunk_index = 0

    log.info(
        "model_node_model_requested",
        extra={
            "msg": f"模型节点请求模型，step_id={step_id}",
            "data": {"messages": [m.model_dump() for m in messages]},
        },
    )
    # part 生命周期收口：把交错的 text/reasoning 流转换为顺序括号化的 part 事件流。
    parts = StreamingPartStateMachine(
        stream_writer, task_id=task_id, run_id=run_id, step_id=step_id
    )

    async for chunk in model.astream(messages):
        # 先于取消检查落盘，确保取消场景也能看到已产出的 chunk。
        _dump_raw_chunk_debug(chunk, chunk_index)
        chunk_index += 1

        if operations.is_current_run_cancelled():
            # 取消直接落定 cancelled 终态，而不是把协作取消误记为失败。
            usage_summary = rc.usage_stats.to_dict()
            log.warning(
                "model_node_cancelled_usage_summary",
                extra={
                    "msg": f"模型流式因取消提前终止，本轮已消耗 token 摘要，step_id={step_id}",
                    "data": {"step_id": step_id, "usage": usage_summary},
                },
            )
            # 取消时把已流式产出但尚未落定终态的部分文本一并写入 final_output，
            # 使复用同一工作流的子 Agent 即便被取消，主 Agent 也能感知其已产出的内容。
            partial_output = (
                content_to_text(_collect_chunk_to_ai_message(chunks).content) if chunks else ""
            )
            parts.finish()
            operations.cancel_run_if_running(
                end_reason="runtime_cancelled",
                usage_stats=rc.usage_stats,
                final_output=partial_output or None,
            )
            return terminal_state(step_count)

        chunks.append(chunk)
        # 提取文本与 reasoning 内容。
        text = content_to_text(chunk.content)
        # 提取 reasoning 内容。
        reasoning = extract_reasoning_content(chunk, thinking_channel)
        #模型把 "\n"、" \n" 单独作为 chunk 时，也要保留
        if text:
            parts.text(text)
        if reasoning and reasoning.strip():
            parts.reasoning(reasoning)
        # tool_call_created 只能在聚合消息后发出；先收口当前文本/推理 part，确保
        # Transport 不会把「reasoning 仍在 running」与后续工具创建绑定在一起。
        if extract_tool_calls(chunk):
            parts.tool_call()
    # 合并 chunk 到 AIMessage。
    ai_message = _collect_chunk_to_ai_message(chunks)
    parts.finish()

    # 以聚合后的 AIMessage 为工具事实唯一来源。流式 tool_call_chunks 的 args 通常是
    # 未闭合 JSON 字符串，不能直接投影；聚合后统一补齐稳定 ID、归一化 args，再按
    # created → pending 顺序发事件，保证 projector 永远先看到 tool-call part。
    tool_calls: list[ToolCall] = []
    for raw_call in ai_message.tool_calls:
        parsed_call = ToolCall.from_from_langchain(raw_call)
        if not parsed_call.tool_name:
            continue
        arguments = parsed_call.arguments if isinstance(parsed_call.arguments, dict) else {}
        tool_calls.append(
            ToolCall(
                tool_name=parsed_call.tool_name,
                arguments=arguments,
                call_id=parsed_call.call_id or f"{run_id}:{step_id}:{len(tool_calls)}",
            )
        )

    context_ai_message = ai_message
    if tool_calls:
        # RuntimeContext 中保存的 AIMessage 必须使用与 event/tool executor 相同的 ID；
        # 否则 RuntimeContextManager 的 ToolMessage 配对与 canonical tool-call part 会
        # 指向两个不同的调用。保留原 ai_message 供 usage/invalid_tool_calls 读取。
        context_ai_message = AIMessage(
            content=ai_message.content,
            additional_kwargs=ai_message.additional_kwargs,
            tool_calls=[
                {
                    "name": tool_call.tool_name,
                    "args": tool_call.arguments,
                    "id": tool_call.call_id,
                    "type": "tool_call",
                }
                for tool_call in tool_calls
            ],
        )
    # 空壳 AIMessage 不进入上下文；这也避免后续误判为可继续推理。
    if _has_content(context_ai_message):
        _runtime_context().add_message(context_ai_message)

    # 累加 usage_metadata 到 run 级共享累加器。
    rc.usage_stats.add_usage_metadata(getattr(ai_message, "usage_metadata", None))

    stream_writer(
        UsageUpdatedEvent(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            **rc.usage_stats.to_dict(),
        )
    )

    for tool_call in tool_calls:
        tool_definition = next(
            (
                definition
                for definition in operations.model_tools
                if definition.name == tool_call.tool_name
            ),
            None,
        )
        stream_writer(
            ToolCallCreatedEvent(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                args=tool_call.arguments,
                presentation=(
                    tool_definition.display.to_dict()
                    if tool_definition is not None and tool_definition.display is not None
                    else {}
                ),
                data=None,
            )
        )
    # 非法工具调用不静默丢弃：决策（纯函数）与执行（下方分支）分离，见 docstring 双轨。
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    # requested_tool 在消费 invalid_tool_calls 前确定，供 REPAIR 块与工具分支共用。
    requested_tool = bool(tool_calls)

    repair_data: list[dict[str, Any]] = []
    repair_message = ""
    if invalid_tool_calls:
        available_tool_names = {tool.name for tool in operations.model_tools}
        result = decide_invalid_tool_handling(
            invalid_tool_calls=invalid_tool_calls, available_tool_names=available_tool_names
        )

        if result[InvalidToolOutcome.IGNORE]:
            # 未命中工具名、无法推断意图：仅记 warning（脱敏 args），不修复、不阻塞。
            log.warning(
                "model_node_invalid_tool_calls_ignored",
                extra={
                    "msg": (
                        "非法工具调用未命中已注册工具名，视为解析噪声忽略，"
                        f"保留合法工具继续执行，step_id={step_id}"
                    ),
                    "data": {
                        "step_id": step_id,
                        "invalid_tool_calls": result[InvalidToolOutcome.IGNORE],
                    },
                },
            )

        repair_data = result[InvalidToolOutcome.REPAIR]
        if repair_data:
            repair_message = build_invalid_tool_call_repair_message(repair_datas=repair_data)
            if not requested_tool:
                output_text = content_to_text(ai_message.content)
                if output_text:
                    repair_message = (
                        f"{repair_message}\n\n上一轮模型的部分输出"
                        f"（请基于此继续完善，勿丢弃）：\n{output_text}"
                    )
                log.warning(
                    "model_node_invalid_tool_calls_no_tool_repair",
                    extra={
                        "msg": (
                            "非法工具调用命中已注册工具名但本轮无合法工具，"
                            f"追加修复提示并回流 model，step_id={step_id}"
                        ),
                        "data": {
                            "step_id": step_id,
                            "invalid_count": len(repair_data),
                            "repair_message_length": len(repair_message),
                        },
                    },
                )
                # 没有合法 tool_calls 时，当前 AIMessage 不携带工具调用，SystemMessage
                # 可以安全地紧随其后；下一轮由 repair_requested 回流 model。
                _runtime_context().add_message(
                    SystemMessage(
                        content=repair_message,
                        additional_kwargs={
                            "cosir_message_kind": TOOL_CALL_REPAIR_MESSAGE_KIND,
                        },
                    )
                )
                return {
                    "step_count": step_count,
                    "repair_requested": True,
                    "requested_tool": False,
                    "final_response": False,
                    "terminal": False,
                    "pending_tool_calls": {},
                    "deferred_repair_message": "",
                    "continuation_error_data": {
                        "error_kind": "invalid_tool_call_repair",
                        "invalid_count": len(repair_data),
                    },
                }

    # 仅当消息有文本或工具调用时才落库，避免空壳消息污染跨轮历史。最终文本消息在
    # complete_run_with_message() 中与 Run 终态同事务写入；工具分支没有 Run 终态，单独写入
    # context 和 tool-call parts 在一个事务内写入。
    # if requested_tool and _has_content(ai_message):
    #     operations.persist_ai_message_with_tool_calls(ai_message, tool_calls, stable_call_ids)
    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "has_tool_calls": requested_tool,
                "tool_count": len(tool_calls),
                "output_text_length": len(ai_message.content),
            },
        },
    )

    if requested_tool:
        log.info(
            "model_node_tool_branch",
            extra={
                "msg": f"模型请求调用 {len(tool_calls)} 个工具，进入工具节点，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_count": len(tool_calls),
                    "has_instruction": bool(ai_message.content),
                    "instruction_length": len(ai_message.content),
                },
            },
        )
        for tool_call in tool_calls:
            stream_writer(
                ToolCallStatusChangedEvent(
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    tool_call_id=tool_call.call_id,
                    status="pending",
                )
            )

        return {
            "step_count": step_count,
            "repair_requested": False,
            "requested_tool": True,
            "final_response": False,
            "terminal": False,
            "deferred_repair_message": repair_message,
            "continuation_error_data": (
                {
                    "error_kind": "invalid_tool_call_repair",
                    "invalid_count": len(repair_data),
                }
                if repair_data
                else None
            ),
            "pending_tool_calls": {
                "tool_calls": [
                    {
                        "tool_name": tool_call.tool_name,
                        "arguments": tool_call.arguments,
                        "call_id": tool_call.call_id,
                    }
                    for tool_call in tool_calls
                ],
                # 文本说明作为 instruction 随工具调用下传，供 tools/observe 节点看到模型意图。
                "instruction": ai_message.content,
            },
        }

    if ai_message.content:  # 没有工具调用，上下文没有变化但有文本 → 最终回答
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
            return terminal_state(step_count)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
            },
        )
        return {
            **terminal_state(step_count, final_response=True),
            "final_text": ai_message.content,
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
    return terminal_state(step_count)



