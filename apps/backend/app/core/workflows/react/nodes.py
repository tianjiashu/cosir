"""ReAct-like 工作流的 LangGraph 节点行为。

本模块只承载节点逻辑，不负责 graph 构建、运行编排或事件翻译。两个节点 ``model`` 与
``tools`` 均为 LangGraph 原生 callable，通过 ``get_config()`` 从运行上下文取出
``operations`` / ``task`` / ``turn`` / ``model``；节点的全部运行时事件（含模型回复增量
``MODEL_OUTPUT_DELTA`` 与思考增量 ``MODEL_THINKING_DELTA``）统一经 ``get_stream_writer()``
写入 ``custom`` 事件流，由编排层 ``astream(stream_mode=["custom"])`` 透传为 ``RuntimeEvent``；
token 由 ``model.astream()`` 产出并在节点内就地翻译为增量事件，不再依赖 ``messages`` 流通道。

状态单一事实来源是 ``Turn``：节点经 ``operations`` 写 **turn** 状态，不再写 task 执行态。
"""

from time import perf_counter

from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.config import get_config, get_stream_writer
from langgraph.types import interrupt

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.llm.langchain_bridge import runtime_to_langchain, tool_calls_from_langchain
from app.models.enums.event_type import EventType
from app.models.payload import (
    FinalResponsePayload,
    ModelCompletedPayload,
    ModelOutputDeltaPayload,
    ModelRequestedPayload,
    ModelThinkingDeltaPayload,
    ModelToolCallPayload,
    RunCancelledPayload,
    RunFailedPayload,
    RunFinishedPayload,
    StepStartedPayload,
)
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.tools.schemas import ToolCall

from .runtime_config import RuntimeConfig
from .state import ReactGraphState


# 内部封装：统一事件写入结构。
def write_event(event_type: EventType, payload: RuntimeEventPayload) -> None:
    writer = get_stream_writer()  # 自定义事件写入器
    writer({"event_type": str(event_type), "payload": payload})


def _runtime_config() -> RuntimeConfig:
    """从 LangGraph 运行上下文取出 ReAct 工作流注入的运行时配置容器。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeConfig`` 放入 config 的 ``runtime_config``；
    节点统一经本函数取出，避免在各节点里用裸字符串 key 重复读取 ``config["configurable"]``。

    返回:
        当前 graph 执行注入的 ``RuntimeConfig`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeConfig。
    return get_config()["configurable"]["runtime_config"]


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
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作（写 turn、跑工具、查状态）
    turn = rc.turn  # 当前 turn 记录
    model = rc.model  # 已构建好的 chat model

    step_count = state.step_count + 1  # 步数 +1（本轮模型步）
    step_id = f"step-{step_count}"  # 步唯一 id
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
    write_event(
        # 请求模型，带上历史消息数
        EventType.MODEL_REQUESTED,
        ModelRequestedPayload(step_id=step_id, message_count=len(state.messages)),
    )

    # 后端在循环结束后需要"完整文本"来做判断和落库，不是为了发给前端
    collected_text: list[str] = []  # 累积输出文本
    chunks: list[AIMessageChunk] = []  # 累积流式分块
    terminal = False  # 是否因取消而提前终止

    # 真正流式调用模型，state.messages 为历史+系统上下文
    async for chunk in model.astream(state.messages):
        # 每收到 chunk 都检查 turn 是否被取消
        if operations.has_turn_status(turn.turn_id, "cancelled"):
            write_event(
                EventType.RUN_CANCELLED,
                RunCancelledPayload(
                    step_id=step_id,
                    status="cancelled",
                    langfuse_trace_id=rc.langfuse_trace_id,
                ),
            )
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
        operations.update_turn_status(turn.turn_id, "cancelled")  # 更新 turn 状态为 cancelled
        return {
            "step_count": step_count,
            "requested_tool": False,
            "final_response": False,
            "terminal": True,  # 终态
            "messages": [],  # 不写消息
            "pending_tool_calls": [],
        }

    ai_message = _finalize_ai_message(chunks)  # 分块合并成完整 AIMessage
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

    write_event(
        EventType.MODEL_COMPLETED,  # 模型产出完成事件
        ModelCompletedPayload(
            step_id=step_id,
            text=output_text,
            tool_calls=[
                ModelToolCallPayload(
                    tool_name=call.tool_name,
                    arguments=call.arguments if isinstance(call.arguments, dict) else {},
                    call_id=call.call_id,
                )
                for call in tool_calls
            ],
        ),
    )

    if requested_tool:  # 模型要求调用工具
        if step_count >= state.max_steps:  # 步数已达上限
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
            operations.update_turn_status(turn.turn_id, "failed")
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
        operations.update_turn_status(turn.turn_id, "completed")  # turn 标完成
        operations.update_turn_response(turn.turn_id, output_text)  # 回复文本落库（历史回看用）
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
    write_event(  # 既没工具调用也没文本 → 模型输出非法
        EventType.RUN_FAILED,
        RunFailedPayload(
            error="invalid_model_output",
            message="Model did not return tool call or final text.",
            langfuse_trace_id=rc.langfuse_trace_id,
        ),
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

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = rc.task  # 任务（工具执行需要 task_id）
    turn = rc.turn  # 当前 turn 记录

    tool_calls = state.pending_tool_calls  # 来自 model 节点写入的待执行工具调用
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    log.info(
        "tools_node_started",
        extra={
            "msg": f"工具节点开始执行，等待审批，step_id={step_id}",
            "data": {"step_id": step_id, "pending_tool_count": len(tool_calls)},
        },
    )

    # 核心：interrupt 暂停 graph，把待审批工具调用交出去；外部审批后用
    # Command(resume=approved_list) 恢复，approved 即为恢复时传入的审批结果。
    approved = interrupt({"tool_calls": tool_calls})
    # 兼容两种恢复值：直接 list 用 list，否则（如误传）回退到原始 tool_calls。
    approved_dicts = tool_calls if not isinstance(approved, list) else approved

    # ★ 取消检查：审批恢复后、工具执行前，若 turn 已被取消则跳过工具执行
    if operations.has_turn_status(turn.turn_id, "cancelled"):
        log.info(
            "tools_node_cancelled",
            extra={
                "msg": f"工具节点恢复后检测到 turn 已取消，跳过工具执行，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        write_event(
            EventType.RUN_CANCELLED,
            RunCancelledPayload(
                step_id=step_id,
                status="cancelled",
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        return {
            "pending_tool_calls": [],
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "messages": [],
        }

    # 把审批结果 dict 重建为内部 ToolCall 值对象（补全 arguments/call_id 默认值）。
    approved_calls = [
        ToolCall(
            tool_name=item["tool_name"],
            arguments=item.get("arguments") or {},
            call_id=item.get("call_id") or "",
        )
        for item in approved_dicts
    ]

    # 真正执行工具（内部会发工具生命周期事件，write_event 作为回调注入）。
    log.info(
        "tools_node_resumed",
        extra={
            "msg": f"审批已恢复，准备执行 {len(approved_calls)} 个工具调用，step_id={step_id}",
            "data": {"step_id": step_id, "approved_count": len(approved_calls)},
        },
    )
    tool_run = operations.run_tool_calls(
        task.task_id,
        approved_calls,
        step_id,
        write_event=write_event,
    )
    observations = tool_run.observations  # 每个工具调用的观察结果

    tool_error_count = state.tool_error_count  # 从 state 继承连续失败计数
    for observation in observations:
        if observation.status == "success":
            tool_error_count = 0  # 成功则清零（连续失败才累计）
        else:
            tool_error_count += 1  # 失败 +1

    success_count = sum(1 for o in observations if o.status == "success")
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具执行完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(observations),
                "success_count": success_count,
                "error_count": len(observations) - success_count,
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Settings.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        log.warning(
            "tools_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_error_count": tool_error_count,
                    "limit": Settings.TOOL_ERROR_LIMIT,
                },
            },
        )
        write_event(
            EventType.RUN_FAILED,
            RunFailedPayload(
                step_id=step_id,
                status="failed",
                error="tool_error_limit_reached",
                tool_name=observations[0].tool_name if observations else "",
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        operations.update_turn_status(turn.turn_id, "failed")
        return {
            "pending_tool_calls": [],
            "tool_error_count": tool_error_count,
            "terminal": True,  # 失败终态
            "messages": [],
        }

    return {
        "pending_tool_calls": [],  # 清空待执行工具调用
        "tool_error_count": tool_error_count,
        # 观察消息转 LangChain 消息追加进 state
        "messages": runtime_to_langchain(tool_run.messages_for_model),
    }
