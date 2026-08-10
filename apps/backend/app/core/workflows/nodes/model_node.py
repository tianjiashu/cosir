"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``task`` / ``turn`` / ``model``，经 ``get_stream_writer()`` 把业务
生命周期事件与流式 token 增量写入自定义事件流；用 ``model.astream()`` 累积 ``AIMessage``，
回复 token 与思考 token 在节点内就地翻译为 ``MODEL_OUTPUT_DELTA`` / ``MODEL_THINKING_DELTA``
事件。根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大步数终止。
状态写入 **turn**。

与工具节点共享的运行时原语见 ``common``；不负责 graph 构建、运行编排或事件翻译。
"""

import json
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from app.config.logging.logger import log
from app.core.llm.langchain_bridge import tool_calls_from_langchain
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.payload import (
    FinalResponsePayload,
    ModelOutputDeltaPayload,
    ModelRequestedPayload,
    ModelThinkingDeltaPayload,
    RunFailedPayload,
    RunFinishedPayload,
    StepStartedPayload,
)
from app.tools.schemas import ToolCall

from ..react.state import ReactGraphState
from .common import _runtime_config, _runtime_context, write_event


def _extract_text(content) -> str:
    """从 LangChain 消息 content 中提取纯文本分片。

    参数:
        content: LangChain 消息的 ``content`` 字段（字符串或分块列表）。

    返回:
        拼接后的纯文本；无法识别时返回空字符串。
    """

    if isinstance(content, str):
        return content  # 普通字符串直接返回
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)  # 列表里直接是字符串
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))  # 多模态文本块 {"type":"text","text":...}
        return "".join(parts)  # 拼接所有文本片段
    return ""  # 其它类型（如图片）返回空


def _has_content(message: AIMessage) -> bool:
    """判断 LangChain 消息是否含有可落库的有效内容。

    参数:
        message: LangChain ``BaseMessage``（通常为 ``AIMessage``）。

    返回:
        消息含非空文本或至少一个工具调用时返回 True，否则返回 False。

    异常:
        无。

    副作用:
        无。
    """

    if _extract_text(message.content).strip():
        return True  # 有文本即视为有效
    tool_calls = getattr(message, "tool_calls", None)
    return bool(tool_calls)  # 有工具调用也视为有效


def _ai_to_runtime_message(ai_message: "AIMessage") -> RuntimeMessage:
    """把合并后的 ``AIMessage`` 转为内部 ``RuntimeMessage`` 以供增量落库。

    参数:
        ai_message: 模型节点合并产出、待写入 graph state 的 ``AIMessage``。

    返回:
        与模型无关的 ``RuntimeMessage``：``metadata`` 中以 **JSON 字符串** 承载
        ``tool_calls``（键 ``tool_calls``），与
        :meth:`app.core.context.runtime_context.RuntimeContext._tool_calls_from_metadata`
        的反序列化契约严格对齐（读取端 ``json.loads``，故此处必须存字符串而非 list）。
        无工具调用时不写入该键。

    异常:
        无。

    副作用:
        无。
    """

    tool_calls = [
        {"name": call.get("name"), "args": call.get("args", {}), "id": call.get("id")}
        for call in (ai_message.tool_calls or [])
    ]
    metadata: dict[str, Any] = (
        {"tool_calls": json.dumps(tool_calls, ensure_ascii=False)} if tool_calls else {}
    )
    return RuntimeMessage(
        role="assistant",
        content_text=_extract_text(ai_message.content),
        metadata=metadata,
    )


def _extract_reasoning_content(chunk) -> str:
    """从 LangChain 消息 chunk 的 additional_kwargs 提取 DeepSeek 思考过程分片。

    ``DeepSeekChatOpenAI`` 已把流式分块中的 ``reasoning_content`` 写入
    ``additional_kwargs["reasoning_content"]``；本函数在不支持 thinking 的模型（该字段缺失）
    时安全返回空串。逐 token 流式场景下，每个 chunk 携带的只是思考片段，由调用方累加到客户端。

    参数:
        chunk: 模型 ``astream`` 产出的 LangChain 消息 chunk。

    返回:
        思考过程文本分片；无则空串。
    """

    additional = getattr(chunk, "additional_kwargs", None)  # 防止无该属性时报错
    if not isinstance(additional, dict):
        return ""  # 非 dict 直接返回空
    value = additional.get("reasoning_content")  # 取思考字段
    return value if isinstance(value, str) else ""  # 非字符串也返回空


def _finalize_ai_message(chunks: list[AIMessageChunk]) -> AIMessage:
    """把累积的 ``AIMessageChunk`` 列表合并为标准的 ``AIMessage``。

    合并时会保留 ``additional_kwargs``（如 DeepSeek 的 ``reasoning_content`` 思考过程），
    否则思考内容会在落库 / 进入 graph state 时被丢弃，导致下游无法将其作为 thinking 事件推送。

    参数:
        chunks: 模型流式产出的分块列表（可能为空）。

    返回:
        可安全存入 graph state 并交给下一步模型调用的 ``AIMessage``。
    """

    merged: AIMessageChunk | None = None
    for chunk in chunks:
        merged = chunk if merged is None else merged + chunk  # LangChain chunk 支持 + 累加
    if merged is None:
        return AIMessage(content="")  # 空输入返回空消息
    # 剥离思考字段：思考内容已在流式阶段作为 MODEL_THINKING_DELTA 推送给前端，
    # 不应随消息回灌给模型（推理模型回灌 reasoning_content 易引发重复思考或协议错误）。
    additional = dict(merged.additional_kwargs) if merged.additional_kwargs else {}
    additional.pop("reasoning_content", None)
    return AIMessage(
        content=merged.content,  # 合并后的文本
        tool_calls=merged.tool_calls or [],  # 工具调用（可能为空）
        additional_kwargs=additional,  # 仅保留非思考的额外字段
        id=getattr(merged, "id", None),  # 消息 id 透传
    )


def _extract_usage_from_chunk(chunk: AIMessageChunk) -> dict[str, int | float] | None:
    """从模型流式分块中提取可用 token usage 元数据。

    不同 provider/SDK 把 usage 放在不同位置：LangChain 标准 ``usage_metadata``、
    OpenAI 适配器的 ``response_metadata.token_usage`` / ``response_metadata.usage`` 等。
    本函数按优先级尝试，返回第一个非空字典；都没有则返回 None。

    参数:
        chunk: 模型 ``astream`` 产出的单个消息分块。

    返回:
        可用的 usage 字典；无则 None。
    """

    usage = getattr(chunk, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return usage
    response_metadata = getattr(chunk, "response_metadata", None)
    if not isinstance(response_metadata, dict):
        return None
    for key in ("token_usage", "usage"):
        candidate = response_metadata.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``task`` / ``turn`` / ``model``，通过
    ``get_stream_writer()`` 把业务生命周期事件与流式 token 增量写入自定义事件流；
    用 ``model.astream()`` 累积 ``AIMessage``，回复 token 与思考 token 在节点内就地翻译为
    ``MODEL_OUTPUT_DELTA`` /
    ``MODEL_THINKING_DELTA`` 事件，由编排层统一透传。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **turn**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）。

    副作用:
        - 经 ``operations.append_runtime_message`` 把本轮 ``AIMessage`` 逐条增量落库；
        - 同步 ``_runtime_context().add_message`` 写回运行时上下文，使下一轮模型节点
          经 ``load_message()`` 能累积看到本轮输出（否则上下文不增长会陷入死循环）；
        - 流式 token / 事件经 ``get_stream_writer`` 透传；状态写入 ``turn``。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作（写 turn、跑工具、查状态）
    turn = rc.turn  # 当前 turn 记录
    model = rc.model  # 已构建好的 chat model

    step_count = state.step_count + 1  # 步数 +1（本轮模型步）
    step_id = f"step-{step_count}"  # 步唯一 id
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "messages": [],
            "pending_tool_calls": [],
        }
    log.info(
        "model_node_started",
        extra={
            "msg": f"模型节点开始执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": step_count,
                "message_count": len(state.messages),
            },
        },
    )
    # 步开始
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
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "messages": [],
            "pending_tool_calls": [],
        }
    write_event(
        # 请求模型，带上历史消息数
        EventType.MODEL_REQUESTED,
        ModelRequestedPayload(step_id=step_id, message_count=len(state.messages)),
    )

    # 后端在循环结束后需要"完整文本"来做判断和落库，不是为了发给前端
    collected_text: list[str] = []  # 累积输出文本
    chunks: list[AIMessageChunk] = []  # 累积流式分块
    terminal = False  # 是否因取消而提前终止

    # 真正流式调用模型，state.messages 为历史+系统上下文。
    messages = _runtime_context().load_message()
    async for chunk in model.astream(messages):
        # 每收到 chunk 都检查 turn 是否被取消
        if operations.is_current_turn_cancelled():
            terminal = True  # 标记提前终止
            log.info(
                "model_node_cancelled",
                extra={
                    "msg": f"模型流式输出期间检测到 turn 已取消，提前终止，step_id={step_id}",
                    "data": {"step_id": step_id},
                },
            )
            break  # 跳出流式循环
        text = _extract_text(chunk.content)  # 抽本 chunk 文本
        if text:
            collected_text.append(text)  # 有文本才累积
            write_event(
                # 回复 token 在节点内就地翻译为增量事件，避免依赖 messages 流
                # 导致完整回复被重复推送
                EventType.MODEL_OUTPUT_DELTA,
                ModelOutputDeltaPayload(step_id=step_id, text=text),
            )
        chunks.append(chunk)  # 所有 chunk 都留着，后面合并成完整消息
        chunk_usage = _extract_usage_from_chunk(chunk)
        if chunk_usage is not None:
            rc.usage_stats.add_message_usage(chunk_usage)
        reasoning = _extract_reasoning_content(chunk)  # 抽思考片段
        # 过滤纯空白分片：DeepSeek 推理流会在词间/段间推送单独的空格或换行 token
        # （如 " "、"\n"、".\n\n"），Python 中非空即 truthy，若仅用 `if reasoning` 判断
        # 会把纯空白分片当作有效思考发射，前端累积后产出空壳"深度思考"块。
        # 仅当去空白后仍有内容才发射，避免无效增量与空壳渲染。
        if reasoning and reasoning.strip():
            write_event(
                # 有思考内容就发思考增量事件，前端可实时渲染“思考中”
                EventType.MODEL_THINKING_DELTA,
                ModelThinkingDeltaPayload(step_id=step_id, text=reasoning),
            )

    if terminal:  # 因取消而终止
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,  # 终态
            "messages": [],  # 不写消息
            "pending_tool_calls": [],
        }

    ai_message = _finalize_ai_message(chunks)  # 分块合并成完整 AIMessage
    # 逐条持久化本轮产生的 assistant 消息（替代 turn 结束后的批落库）。
    # 仅当消息有文本或工具调用时才落库，避免空壳消息污染跨轮历史。
    if _has_content(ai_message):
        operations.append_runtime_message(_ai_to_runtime_message(ai_message))
        # 同步写回运行时上下文，使下一模型步经 _runtime_context().load_message()
        # 能读到本轮累积的 assistant 消息，否则模型每步都看到不变的首轮快照，
        # 会陷入「相同上下文→相同输出」的死循环。
        _runtime_context().add_message(ai_message)
    # 把 LangChain 的 tool_calls 转成内部 ToolCall 值对象
    tool_calls: list[ToolCall] = tool_calls_from_langchain(ai_message.tool_calls or [])
    output_text = "".join(collected_text).strip()  # 拼接文本并去首尾空白
    requested_tool = bool(tool_calls)  # 是否要调工具
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

    if requested_tool:  # 模型要求调用工具
        if step_count >= state.max_steps:  # 步数已达上限
            failed_turn = operations.fail_turn_if_running(
                turn.turn_id, end_reason="max_steps_reached"
            )
            if failed_turn is None:
                log.info(
                    "model_node_max_steps_terminal_race_lost",
                    extra={
                        "msg": (
                            f"最大步数失败落定时 turn 已非 running，"
                            f"跳过失败事件，step_id={step_id}"
                        ),
                        "data": {"step_id": step_id, "turn_id": turn.turn_id},
                    },
                )
                return {
                    "step_count": step_count,
                    "requested_tool": False,
                    "final_response": False,
                    "terminal": True,
                    "messages": [],
                    "pending_tool_calls": [],
                }
            log.warning(
                "model_node_max_steps",
                extra={
                    "msg": f"已达到最大步数上限，停止调用工具，step_id={step_id}",
                    "data": {
                        "step_id": step_id,
                        "step_count": step_count,
                        "max_steps": state.max_steps,
                    },
                },
            )
            # 超限失败
            write_event(
                EventType.RUN_FAILED,
                RunFailedPayload(
                    status="failed",
                    error="max_steps_reached",
                    langfuse_trace_id=rc.langfuse_trace_id,
                ),
            )
            return {
                "step_count": step_count,
                "requested_tool": False,
                "final_response": False,
                "terminal": True,
                "messages": [ai_message],  # 把 ai_message 写回 state，checkpoint 可保留
                "pending_tool_calls": [],
            }
        log.info(
            "model_node_tool_branch",
            extra={
                "msg": f"模型请求调用 {len(tool_calls)} 个工具，进入工具节点，step_id={step_id}",
                "data": {"step_id": step_id, "tool_count": len(tool_calls)},
            },
        )
        return {
            "step_count": step_count,
            "requested_tool": True,  # 进入工具分支
            "final_response": False,
            "terminal": False,  # 非终态，graph 会继续到 tools 节点
            "messages": [ai_message],
            # 待执行工具调用交给 tools 节点
            "pending_tool_calls": [
                {
                    "tool_name": call.tool_name,
                    "arguments": call.arguments if isinstance(call.arguments, dict) else {},
                    "call_id": call.call_id,
                }
                for call in tool_calls
            ],
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
            return {
                "step_count": step_count,
                "requested_tool": False,
                "final_response": False,
                "terminal": True,
                "messages": [],
                "pending_tool_calls": [],
            }
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
        # 整个 run 结束：计算耗时并汇总 token
        duration_ms = int((perf_counter() - rc.start_time) * 1000)
        usage = rc.usage_stats.to_dict()
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
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": True,  # 终态最终回复
            "terminal": True,
            "messages": [ai_message],
            "pending_tool_calls": [],
            "final_text": output_text,  # 供上层取最终回复
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
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "messages": [],
            "pending_tool_calls": [],
        }
    write_event(  # 既没工具调用也没文本 → 模型输出非法
        EventType.RUN_FAILED,
        RunFailedPayload(
            error="invalid_model_output",
            message="Model did not return tool call or final text.",
            langfuse_trace_id=rc.langfuse_trace_id,
        ),
    )
    return {
        "step_count": step_count,
        "requested_tool": False,
        "final_response": False,
        "terminal": True,
        "messages": [ai_message],
        "pending_tool_calls": [],
    }
