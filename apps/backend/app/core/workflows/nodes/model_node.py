"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``turn`` / ``model``，经 ``get_stream_writer()`` 把业务
生命周期事件与流式 token 增量写入自定义事件流；用 ``model.astream()`` 累积 ``AIMessage``，
回复 token 与思考 token 在节点内就地翻译为 ``MODEL_OUTPUT_DELTA`` / ``MODEL_THINKING_DELTA``
事件。根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大步数终止。
状态写入 **turn**。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_files`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支的 ``FINAL_RESPONSE``），但模型这段说明并非丢弃——
它会经 ``MODEL_OUTPUT_DELTA`` 流式推给前端、经 ``RuntimeContextManager.add_message``
落库进历史上下文，并在进入工具分支时作为 ``instruction`` 键随 ``pending_tool_calls``
下传给 ``tools`` / ``observe`` 节点，使下游执行与错误排查能看到模型当时的意图。

模型侧数据处理辅助（思考通道抽取、chunk 组装、chunk debug 落盘）已拆为独立模块
（``thinking_extractor`` / ``chunk_assembler`` / ``debug_dump``），本模块仅 import 使用；
与运行上下文强绑定的 ``_estimate_run_cost`` 保留在本模块。上下文占用事件（``CONTEXT_USAGE``）
已由 ``ContextUsageEventEmitter`` 订阅机制在消息变更时自动发出，本模块不再手动触发。
节点共享运行时原语见 ``common``。
"""

from time import perf_counter
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
    build_run_failed_payload,
    emit_run_cancelled,
    terminal_state,
    write_event,
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
from app.models.enums.event_type import EventType
from app.models.payload import (
    FinalResponsePayload,
    ModelOutputDeltaPayload,
    ModelRequestedPayload,
    ModelThinkingDeltaPayload,
    RunFinishedPayload,
    StepStartedPayload,
)
from app.service.llm.cost_estimator import UsageBreakdown, estimate_cost
from app.tools.schemas import ToolCall
from app.utils.message_content import content_to_text

from ..react.state import ReactGraphState


def _estimate_run_cost(rc, usage: dict[str, int]) -> float | None:
    """估算本 run 的模型调用成本（美分）；无法估算返回 None。

    参数:
        rc: 当前 ``RuntimeConfig``（取 ``llm_config.model_name``）。
        usage: ``TurnUsageStats.to_dict()`` 产出的扁平 token 统计。

    返回:
        估算成本（美分，浮点）；``llm_config`` 缺失或模型价格不可估算时返回 None。

    异常:
        无。

    副作用:
        无。
    """
    if rc.llm_config is None:
        return None
    model_name = rc.llm_config.model_name
    return estimate_cost(
        UsageBreakdown(
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_hit_tokens=usage["cache_hit_tokens"],
        ),
        model_name,
    )


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``turn`` / ``model``，通过 ``get_stream_writer()``
    把业务生命周期事件与流式 token 增量写入自定义事件流；用 ``model.astream()`` 累积
    ``AIMessage``，回复 token 与思考 token 在节点内就地翻译为 ``MODEL_OUTPUT_DELTA`` /
    ``MODEL_THINKING_DELTA`` 事件，由编排层统一透传。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **turn**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）；当本次推理
        已超配额（``step_count > max_steps``）时不发起推理，直接返回
        ``_finalize_max_steps`` 的终态 patch。

    副作用:
        - 发起推理前若本次已超配额，直接调用 ``_finalize_max_steps`` 收口终态（发
          ``RUN_FAILED``、把 turn 标 failed），不再触发推理；
        - 经 ``_runtime_context().add_message`` 把本轮 ``AIMessage`` 落库并写回内存
          （``RuntimeContextManager`` 唯一写入入口），使下一模型步能累积看到本轮输出；
        - 流式 token / 事件经 ``get_stream_writer`` 透传；状态写入 ``turn``；
        - 非法输出终态发 ``RUN_FAILED``（携带 usage 摘要）；请求前/流式中取消均发
          ``RUN_CANCELLED``（携带 usage）；
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

    # thinking 通道与回传开关来自本次解析的 LLMRuntimeConfig（无则为空通道 + 默认回传）。
    llm_config = rc.llm_config
    thinking_channels: tuple[str, ...] = (
        llm_config.thinking_channels if llm_config is not None else ()
    )
    thinking_roundtrip = llm_config.thinking_roundtrip if llm_config is not None else True

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
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        # 请求前取消同样走统一终态并发 RUN_CANCELLED，与流式中取消/工具取消保持事件一致，
        # 否则前端 StatusBadge 无法感知取消。请求前未调用模型，usage 为零值。
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
    write_event(
        EventType.STEP_STARTED,
        StepStartedPayload(step_id=step_id, kind="model", index=step_count),
    )
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_model_requested",
            extra={
                "msg": f"模型请求事件前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        # 同上一检查点：请求事件前取消走统一终态并发 RUN_CANCELLED。
        emit_run_cancelled(rc, step_id)
        return terminal_state(step_count)
    write_event(
        EventType.MODEL_REQUESTED,
        ModelRequestedPayload(step_id=step_id, message_count=len(messages)),
    )

    # 流式阶段每 chunk 就地翻译为 MODEL_OUTPUT_DELTA 事件（text 取自该 chunk 的
    # content 增量）；chunks 攒结构化分块合并成 AIMessage 供解析 tool_calls 与
    # 提取最终正文（output_text 从合并后的 content 统一取，见下）。
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
            # 取消发 RUN_CANCELLED（携带 usage）而非 RUN_FAILED，避免与取消流其它信号重复。
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
        reasoning = _extract_reasoning_content(chunk, thinking_channels)
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
            write_event(
                # 就地翻译为增量事件，避免经 messages 流导致完整回复被重复推送。
                EventType.MODEL_OUTPUT_DELTA,
                ModelOutputDeltaPayload(step_id=step_id, text=text),
            )
        if reasoning and reasoning.strip():
            write_event(
                EventType.MODEL_THINKING_DELTA,
                ModelThinkingDeltaPayload(step_id=step_id, text=reasoning),
            )

    ai_message = _collect_chunk_to_ai_message(
        chunks,
        thinking_channels=thinking_channels,
        thinking_roundtrip=thinking_roundtrip,
    )
    # 单一来源：usage 只在模型调用产出 ai_message 后从其 usage_metadata 累加一次。
    # ai_message.usage_metadata 是 LangChain 对各流式 chunk 求和无重复后的完整快照，
    # 不再逐 chunk 解析（消除双重口径与键名偏差）。本对象为 turn 级共享累加器，
    # REPAIR 回流的多次模型调用会依次累加，各步末态快照互不覆盖。
    rc.usage_stats.add_usage_metadata(getattr(ai_message, "usage_metadata", None))

    tool_calls: list[ToolCall] = [ToolCall.from_from_langchain(call) for call in ai_message.tool_calls]
    # 非法工具调用不静默丢弃：决策（纯函数）与执行（下方分支）分离，见 docstring 双轨。
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    # requested_tool 在消费 invalid_tool_calls 前确定，供 REPAIR 块与工具分支共用。
    requested_tool = bool(tool_calls)
    # 模型已产出的正文统一取合并后 content（与 _has_content 落库判定同源），避免与
    # 流式阶段就地推送的 MODEL_OUTPUT_DELTA 事件出现两套正文口径。对文本块，逐 chunk
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
                "call_id": call.call_id,
                "instruction": instruction,
            }
            for call in tool_calls
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
        completed_turn = operations.complete_turn_if_running(turn.turn_id, output_text)
        if completed_turn is None:
            log.info(
                "model_node_final_response_terminal_race_lost",
                extra={
                    "msg": f"最终回复落定时 turn 已非 running，跳过完成事件，step_id={step_id}",
                    "data": {"step_id": step_id, "turn_id": turn.turn_id},
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
        write_event(
            EventType.FINAL_RESPONSE,
            FinalResponsePayload(text=output_text, step_id=step_id, status="completed"),
        )
        # 整个 run 结束：计算耗时、汇总 token 并估算成本。
        duration_ms = int((perf_counter() - rc.start_time) * 1000)
        usage = rc.usage_stats.to_dict()
        cost_cents = _estimate_run_cost(rc, usage)
        write_event(
            EventType.RUN_FINISHED,
            RunFinishedPayload(
                status="completed",
                step_id=step_id,
                duration_ms=duration_ms,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
                cache_hit_tokens=usage["cache_hit_tokens"],
                cache_miss_tokens=usage["cache_miss_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                cost_cents=cost_cents,
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
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
    failed_turn = operations.fail_turn_if_running(turn.turn_id, end_reason="invalid_model_output")
    if failed_turn is None:
        log.info(
            "model_node_invalid_output_terminal_race_lost",
            extra={
                "msg": f"非法模型输出失败落定时 turn 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        return terminal_state(step_count)
    write_event(  # 既没工具调用也没文本 → 模型输出非法
        EventType.RUN_FAILED,
        build_run_failed_payload(
            step_id,
            "invalid_model_output",
            usage=rc.usage_stats,
            langfuse_trace_id=rc.langfuse_trace_id,
        ),
    )
    return terminal_state(step_count)
