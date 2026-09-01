"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``turn`` / ``model``，将模型文本与 reasoning 增量直接交给
``RuntimeOperations`` 的 canonical writer；用 ``model.astream()`` 累积 ``AIMessage``，
根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大步数终止。
状态写入 **turn**。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_files`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支），但模型这段说明并非丢弃——
它会经 canonical conversation facts 写入、经 ``RuntimeContextManager.add_message``
落库进历史上下文，并在进入工具分支时作为 ``instruction`` 键随 ``pending_tool_calls``
下传给 ``tools`` / ``observe`` 节点，使下游执行与错误排查能看到模型当时的意图。

模型侧数据处理辅助（思考通道抽取、chunk 组装、chunk debug 落盘）已拆为独立模块
（``thinking_extractor`` / ``chunk_assembler`` / ``debug_dump``），本模块仅 import 使用；
节点共享运行时原语见 ``common``。
"""

from typing import Any

from langchain_core.messages import AIMessageChunk, SystemMessage

from app.config.logging.logger import log
from app.core.workflows.nodes.finalize_max_steps import _finalize_max_steps
from app.core.workflows.nodes.helper.chunk_assembler import (
    _collect_chunk_to_ai_message,
    _has_content,
)
from app.core.workflows.nodes.helper.common import (
    _runtime_config,
    _runtime_context,
    emit_run_cancelled,
    terminal_state,
)
from app.core.workflows.nodes.helper.debug_dump import (
    _dump_merged_chunk_debug,  # noqa: F401  # 测试经 model_node._dump_merged_chunk_debug 访问
    _dump_raw_chunk_debug,
)
from app.core.workflows.nodes.helper.invalid_tool_call import (
    InvalidToolOutcome,
    build_invalid_tool_call_repair_message,
    decide_invalid_tool_handling,
)
from app.core.workflows.nodes.helper.thinking_extractor import (
    _extract_reasoning_content,
    _should_strip_reasoning_content,  # noqa: F401  # 测试经 model_node._should_strip_reasoning_content 访问
)
from app.tools.schemas import ToolCall
from app.utils.message_content import content_to_text

from ..react.state import ReactGraphState


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``turn`` / ``model``，将模型文本与 reasoning 增量
    直接写入 canonical conversation facts；用 ``model.astream()`` 累积
    ``AIMessage``。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **turn**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）；当本次推理
        已超配额（``step_count > max_steps``）时不发起推理，直接返回
        ``_finalize_max_steps`` 的终态 patch。

    副作用:
        - 发起推理前若本次已超配额，直接调用 ``_finalize_max_steps`` 收口终态（发
          把 turn 标记为 failed），不再触发推理；
        - 经 ``_runtime_context().add_message`` 把本轮 ``AIMessage`` 落库并写回内存
          （``RuntimeContextManager`` 唯一写入入口），使下一模型步能累积看到本轮输出；
        - 模型文本与 reasoning 增量经 ``RuntimeOperations`` 写入 canonical facts；状态写入
          ``turn``；
        - 非法输出经 ``RuntimeOperations`` 落定失败；请求前/流式中取消经同一门面落定取消；
        - ``invalid_tool_calls`` 按双轨消费：未命中工具名的 ``IGNORE`` 仅记 warning；
          命中工具名的 ``REPAIR`` 在 ``requested_tool`` 为真（情形 a）时把修复提示作为独立
          state 字段 ``deferred_repair_message`` 回传（不 return，由 observe 节点延后注入），
          为假（情形 b）时写 ``SystemMessage`` 并返回非终态 patch 回流 model 重试
          （靠 ``max_steps`` 兜底）；回流时把模型已产出的非空 ``output_text`` 追加进修复提示，
          避免其被静默丢弃。
    """

    rc = _runtime_config()
    operations = rc.operations
    turn = rc.turn
    model = rc.model
    thinking_channel = rc.thinking_channel
    thinking_roundtrip = rc.thinking_roundtrip

    step_count = state.step_count + 1
    # P1-5 提前拦截：本次推理若已超配额（step_count > max_steps），不发起推理，直接调用
    # _finalize_max_steps 统一收口终态。这样「超配额」不再触发推理、也不会因该次推理产出
    # 非法输出而滑落 invalid_model_output / completed 终态——终态分类恒为
    # max_steps_reached。覆盖所有进入本节点的路径（START / observe 回流 / REPAIR 自回流），
    # 是唯一拦截点。
    if step_count > state.max_steps:
        return await _finalize_max_steps(state, step_count=step_count)
    step_id = f"step-{step_count}"
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.id},
            },
        )
        # 请求前取消同样走统一 canonical 终态，与流式中取消/工具取消保持语义一致。
        emit_run_cancelled(rc, step_id)
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
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_model_requested",
            extra={
                "msg": f"模型请求事件前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.id},
            },
        )
        # 同上一检查点：请求前取消走统一 canonical 终态。
        emit_run_cancelled(rc, step_id)
        return terminal_state(step_count)
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

    async for chunk in model.astream(messages):
        # 先于取消检查落盘，确保取消场景也能看到已产出的 chunk。
        _dump_raw_chunk_debug(chunk, chunk_index)
        chunk_index += 1

        if operations.is_current_turn_cancelled():
            # 取消直接落定 cancelled 终态，而不是把协作取消误记为失败。
            usage_summary = rc.usage_stats.to_dict()
            log.warning(
                "model_node_cancelled_usage_summary",
                extra={
                    "msg": f"模型流式因取消提前终止，本轮已消耗 token 摘要，step_id={step_id}",
                    "data": {"step_id": step_id, "usage": usage_summary},
                },
            )
            emit_run_cancelled(rc, step_id)
            return terminal_state(step_count)

        chunks.append(chunk)
        text = content_to_text(chunk.content)
        reasoning = _extract_reasoning_content(chunk, thinking_channel)
        # 诊断日志：每个流式 chunk 的增量体量。若多数 chunk 的 content_len=0 仅在末 chunk
        # 出现整段文本，说明模型/provider 未做逐 token 流式，前端表现为「整块出现、无流式感」。
        log.info(
            "model_node_stream_chunk",
            extra={
                "msg": (
                    f"流式 chunk 已处理，chunk_index={chunk_index}，"
                    f"content_len={len(text)}，reasoning_len={len(reasoning or '')}，"
                    f"step_id={step_id}"
                ),
                "data": {
                    "step_id": step_id,
                    "chunk_index": chunk_index,
                    "content_len": len(text),
                    "reasoning_len": len(reasoning or ""),
                    "emitted_output_delta": bool(text),
                    "emitted_thinking_delta": bool(reasoning and reasoning.strip()),
                },
            },
        )
        if text:
            operations.append_assistant_text(text)
        if reasoning and reasoning.strip():
            operations.append_assistant_reasoning(reasoning)

    ai_message = _collect_chunk_to_ai_message(
        chunks,
        thinking_channel=thinking_channel,
        thinking_roundtrip=thinking_roundtrip,
    )
    # 单一来源：usage 只在模型调用产出 ai_message 后从其 usage_metadata 累加一次。
    # ai_message.usage_metadata 是 LangChain 对各流式 chunk 求和无重复后的完整快照，
    # 不再逐 chunk 解析（消除双重口径与键名偏差）。本对象为 turn 级共享累加器，
    # REPAIR 回流的多次模型调用会依次累加，各步末态快照互不覆盖。
    rc.usage_stats.add_usage_metadata(getattr(ai_message, "usage_metadata", None))

    tool_calls: list[ToolCall] = [
        ToolCall.from_from_langchain(call) for call in ai_message.tool_calls
    ]
    # 非法工具调用不静默丢弃：决策（纯函数）与执行（下方分支）分离，见 docstring 双轨。
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    # requested_tool 在消费 invalid_tool_calls 前确定，供 REPAIR 块与工具分支共用。
    requested_tool = bool(tool_calls)
    # 模型已产出的正文统一取合并后 content（与 _has_content 落库判定同源），避免与
    # 流式阶段写入的文本与合并后正文需要保持同一口径。对文本块，逐 chunk
    # 提取拼接与合并后整体提取等价；末 chunk 一次性给 content 也能被捕获，不会因 delta
    # 通道未逐 chunk 下传而误判无正文。供修复提示/最终回答/instruction 共用。
    output_text = content_to_text(ai_message.content).strip()

    repair_message: str | None = None
    repair_data: list[dict[str, Any]] = []

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

        if result[InvalidToolOutcome.REPAIR]:
            # 命中已注册工具名，视为真实调用意图、仅字段非法：按 requested_tool 二维分流。
            # 情形 a 有合法工具时延后注入修复提示（不 return）；情形 b 无合法工具时写
            # SystemMessage 并返回非终态 patch 回流 model 重试（靠 max_steps 兜底）。
            repair_data = result[InvalidToolOutcome.REPAIR]

            repair_message = build_invalid_tool_call_repair_message(repair_datas=repair_data)

            if not requested_tool:
                # 情形 b 须落库修复提示，保证崩溃恢复后仍可重试。
                # 模型可能已输出部分正文/意图文本，若直接回流重试会把它静默丢弃；这里把非空
                # output_text 作为「上一轮的部分输出」追加进修复提示，供模型重试时参考保留。
                if output_text:
                    repair_message = (
                        f"{repair_message}\n\n上一轮模型的部分输出"
                        f"（请基于此继续完善，勿丢弃）：\n{output_text}"
                    )
                log.warning(
                    "model_node_invalid_tool_calls_no_tool_deferred",
                    extra={
                        "msg": (
                            "非法工具调用命中已注册工具名但本轮无合法工具，"
                            f"修复提示直接注入并回流 model 重试，step_id={step_id}"
                        ),
                        "data": {
                            "step_id": step_id,
                            "repair_message_length": len(repair_message or ""),
                        },
                    },
                )
                _runtime_context().add_message(SystemMessage(content=repair_message))
                return {
                    "step_count": step_count,
                    "repair_requested": True,
                    "requested_tool": False,
                    "final_response": False,
                    "terminal": False,
                    "pending_tool_calls": [],
                    "continuation_error_data": None,
                }

    # 仅当消息有文本或工具调用时才落库，避免空壳消息污染跨轮历史。
    if _has_content(ai_message):
        # 落库后写回内存，使下一模型步经 load_message() 能读到本轮输出，避免上下文不增长死循环。
        _runtime_context().add_message(ai_message)
    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "has_tool_calls": requested_tool,
                "tool_count": len(tool_calls),
                "output_text_length": len(output_text),
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
                    "has_instruction": bool(output_text),
                    "instruction_length": len(output_text),
                },
            },
        )
        # 文本说明作为 instruction 随工具调用下传，供 tools/observe 节点看到模型意图。
        instruction = output_text if output_text else ""
        # repair_message 是 str；repair_data 非空才确有 REPAIR 项需延后注入。
        deferred_repair_content = str(repair_message) if repair_data else ""
        # pending_tool_calls 只承载单条工具的 tool_name/arguments/call_id/instruction 四键；
        # 延后 REPAIR 修复提示不再挂在工具数组上，改为经独立 state 字段
        # deferred_repair_message 下传，由 observe 节点工具结果处理后统一注入并清空，
        # 避免与单条工具强绑定。
        pending_tool_calls = [
            {
                "tool_name": call.tool_name,
                "arguments": call.arguments,
                # Some providers omit an id. Generate a stable per-run/per-step id so
                # parallel calls of the same tool cannot collapse into one fact.
                "call_id": call.call_id or f"{turn.id}:{step_id}:{index}",
                "instruction": instruction,
            }
            for index, call in enumerate(tool_calls)
        ]
        return {
            "step_count": step_count,
            "repair_requested": False,
            "requested_tool": True,
            # 延后 REPAIR 修复提示作为独立 state 字段回传（情形 a 有合法工具时非空）；
            # observe 节点工具结果处理后注入并清空。
            "deferred_repair_message": deferred_repair_content,
            # 仅回传计数而非未脱敏原始 invalid_tool_call，防止原文外泄且避免 _finalize_max_steps
            # 以 dict() 展开 list 抛 ValueError。
            "continuation_error_data": (
                {
                    "error_kind": "invalid_tool_call_repair",
                    "invalid_count": len(repair_data),
                }
                if repair_data
                else None
            ),
            "final_response": False,
            "terminal": False,
            "pending_tool_calls": pending_tool_calls,
        }

    if output_text:  # 没有工具调用但有文本 → 最终回答
        completed_turn = operations.complete_turn_if_running(turn.id, output_text)
        if completed_turn is None:
            log.info(
                "model_node_final_response_terminal_race_lost",
                extra={
                    "msg": f"最终回复落定时 turn 已非 running，跳过完成事件，step_id={step_id}",
                    "data": {"step_id": step_id, "turn_id": turn.id},
                },
            )
            return terminal_state(step_count)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(output_text)},
            },
        )
        return {
            **terminal_state(step_count, final_response=True),
            "final_text": output_text,
        }

    log.warning(
        "model_node_invalid_output",
        extra={
            "msg": f"模型既未返回工具调用也无有效文本，判定为非法输出，step_id={step_id}",
            "data": {"step_id": step_id, "output_text_length": len(output_text)},
        },
    )
    failed_turn = operations.fail_turn_if_running(turn.id, end_reason="invalid_model_output")
    if failed_turn is None:
        log.info(
            "model_node_invalid_output_terminal_race_lost",
            extra={
                "msg": f"非法模型输出失败落定时 turn 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.id},
            },
        )
        return terminal_state(step_count)
    return terminal_state(step_count)
