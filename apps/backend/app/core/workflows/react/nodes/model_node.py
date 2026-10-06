"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量写入 workflow custom
stream；用 ``model.astream()`` 消费流式输出（草稿由 ``RuntimeContextManager`` 累积并收口成完整
``AIMessage``），再把原始工具请求交给 ``tools`` 节点分类处理。普通推理预算耗尽或工具拒绝次数
超限时切换到无工具最终回答；Run 状态经 ``WorkflowOperations`` 落到 ``ConversationRun``。

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

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
)
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.react.node_helper.common import (
    _runtime_config,
    _runtime_context,
    end_state,
)
from app.core.workflows.react.node_helper.finalize_max_steps import _finalize_max_steps
from app.core.workflows.react.node_helper.model_chunk import ModelChunkProcessor
from app.core.workflows.react.node_helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.vision_input import resolve_messages_for_model
from app.utils.message_content import content_to_text

# ``finish_reason`` 是 Provider 语义，不直接等同于工作流终态。不同兼容层可能使用
# ``stop``、``end`` 或 ``end_turn`` 表示正常文本结束；在 model 节点内做最小归一化，避免
# 把 provider-specific 字符串扩散到 graph edge 与终态写入逻辑。


def _build_continuation_prompt(finish_reason: str | None) -> str | None:
    """为未完成的模型输出构造一次模型可消费的继续提示。

    ``length`` 类原因（``Constant.Workflow.CONTINUATION_FINISH_REASONS``）只表示本轮达到输出
    上限，已有文本本身可被模型自然延续，因此**不注入提示**（返回 ``None``）；缺失或未知原因
    表示 Provider/适配器没有提供可确认的正常结束信号，此时必须显式追加继续提示，否则模型无
    从得知上一轮未完成。

    两类情况都不应把已有文本直接标记为最终回答，是否注入提示由本函数的返回值决定。

    参数:
        finish_reason: 已归一化的 Provider 完成原因，可为 ``None``。

    返回:
        追加到 canonical context 的 ``SystemMessage`` 文本；``length`` 类截断返回 ``None``，
        表示本轮无需注入提示、直接由调用方路由回模型节点。

    异常:
        无。

    副作用:
        无。
    """

    if finish_reason in Constant.Workflow.CONTINUATION_FINISH_REASONS:
        return None
    reason_text = finish_reason or "missing"
    return (
        "Your previous response did not provide a recognized completion signal "
        f"(finish_reason={reason_text}). Treat it as incomplete, continue from where it stopped, "
        "do not repeat completed content, and finish the answer."
    )


async def _model_node(state: ReactGraphState) -> dict:
    """流式调用模型，并把模型输出转换为工作流路由结果。

    普通阶段使用绑定工具的模型；最终回答阶段使用不绑定工具的模型。工具调用原样交给
    ``tools`` 节点分类和执行，本节点不创建工具生命周期事实。超过普通模型步数时转入一次
    最终回答阶段；只有模型返回正常结束原因且有文本时才完成 Run。

    参数:
        state: 当前 graph state，包含步数、工具请求和最终回答阶段标志。

    返回:
        合并到 graph state 的步数、路由、模型指令或工具请求增量。

    异常:
        Exception: 模型调用、上下文读写或流式收口失败时向上传播，由 workflow 收敛 Run。

    副作用:
        增量持久化 assistant 草稿，将文本与 reasoning 写入 workflow stream；完成时把模型消息
        收口到运行时上下文。检测到取消时落定取消状态并中断当前图节点。
    """

    rc = _runtime_config()
    operations = rc.operations
    model = rc.final_model if state.final_answer_only else rc.model
    thinking_channel = rc.thinking_channel
    chunk_processor = ModelChunkProcessor(thinking_channel)
    stream_writer = get_stream_writer()

    task_id = operations.get_current_task().id
    run_id = operations.get_current_run().id

    step_count = state.step_count + 1
    # 普通推理耗尽后仍保留一次无工具最终回答请求。
    if step_count > state.max_steps and not state.final_answer_only:
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
    workspace_id = operations.get_current_workspace().id
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    task_space = task_runtime_spaces.get_or_create(task_id)
    # load_message() 出口已归一化 assistant 消息，此处直接取用，不再重复 sanitize。
    messages: list[BaseMessage] = _runtime_context().load_message()

    for system_message in task_space.take_deferred_system_messages(run_id=run_id):
        # 如果存在run_id，说明是与run绑定的system message，需要核对run_id是否一致
        message_run_id = system_message.additional_kwargs.get("run_id")
        if message_run_id is not None and message_run_id != run_id:
            continue
        _runtime_context().add_message(system_message)
        messages.append(system_message)

    if state.final_answer_only:
        final_instruction = SystemMessage(
            content=(
                "工具调用阶段已经结束。现在只根据当前对话和已有工具结果，直接给出用户可见的最终回答。"
                "不要调用工具，不要描述内部路由或重试过程；信息不足时明确说明已知事实与限制。"
            ),
            additional_kwargs={"run_id": run_id},
        )
        _runtime_context().add_message(final_instruction)
        messages.append(final_instruction)

    messages = await asyncio.to_thread(
        resolve_messages_for_model,
        messages,
        workspace_id=workspace_id,
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
    # part 生命周期收口：把交错的 text/reasoning 流转换为顺序括号化的 part 事件流。
    parts = StreamingPartStateMachine(
        stream_writer, task_id=task_id, run_id=run_id, step_id=step_id
    )

    requested_tool_calls: list[dict[str, object]] = []

    async for chunk in model.astream(messages):
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
            _runtime_context().flush_message_chunk(stream_id=step_id, run_id=run_id, mode="cancel")
            parts.finish()
            operations.cancel_run_if_running(
                usage_stats=rc.usage_stats, final_output="user_cancelled"
            )
            interrupt({"reason": "user_cancelled"})

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
            # 这里只收口文本 part；工具调用的分类和生命周期统一由 tools 节点负责。
            parts.tool_call()

    parts.finish()
    ai_message: AIMessage = _runtime_context().flush_message_chunk(
        stream_id=step_id, run_id=run_id, mode="complete"
    )

    finish_reason = chunk_processor.extract_finish_reason(ai_message)

    # 累加 usage_metadata 到 run 级共享累加器。
    rc.usage_stats.add_usage_metadata(getattr(ai_message, "usage_metadata", None))

    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    requested_tool_calls = [dict(call) for call in ai_message.tool_calls if isinstance(call, dict)]
    invalid_requests = [dict(call) for call in invalid_tool_calls if isinstance(call, dict)]
    tool_request = {
        "tool_calls": requested_tool_calls,
        "invalid_tool_calls": invalid_requests,
    }

    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(requested_tool_calls),
                "invalid_count": len(invalid_requests),
                "output_text_length": len(ai_message.content),
                "finish_reason": finish_reason,
            },
        },
    )

    has_tool_request = bool(requested_tool_calls or invalid_requests)
    if has_tool_request and not state.final_answer_only:
        return {
            "step_count": step_count,
            "next_node": ReactRoute.TOOLS,
            "instruction": ai_message.content if isinstance(ai_message.content, str) else "",
            "tool_request": tool_request,
        }

    if state.final_answer_only and has_tool_request:
        final_output = "最终回答阶段仍请求调用工具，未能生成有效最终回答。"
        failed_run = operations.fail_run_if_running(
            end_reason="invalid_model_output",
            usage_stats=rc.usage_stats,
            final_output=final_output,
        )
        if failed_run is None:
            log.info(
                "model_node_final_answer_tool_request_terminal_race_lost",
                extra={
                    "msg": "最终回答阶段仍请求工具时 Run 已非运行态",
                    "data": {"step_id": step_id, "run_id": run_id},
                },
            )
        return {**end_state(step_count), "final_text": final_output}

    final_content = content_to_text(ai_message.content).strip()
    if finish_reason in Constant.Workflow.NORMAL_FINISH_REASONS and final_content:
        # 没有工具调用且 Provider 明确报告正常结束 → 普通最终文本，必要时继续走结构化收口。
        final_answer = final_content
        if rc.structured_output is not None and final_content:
            # 普通最终答复作为结构化节点的上下文候选；这里只交接控制流，不提前完成 Run。
            return {
                "step_count": step_count,
                "next_node": ReactRoute.STRUCTURED_OUTPUT,
                "instruction": "",
                "final_text": final_content,
            }
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
            return end_state(step_count)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
            },
        )
        return {
            **end_state(step_count),
            "final_text": final_content,
        }

    if state.final_answer_only:
        final_output = "模型未能在最终回答阶段返回有效文本。"
        failed_run = operations.fail_run_if_running(
            end_reason="invalid_model_output",
            usage_stats=rc.usage_stats,
            final_output=final_output,
        )
        if failed_run is None:
            log.info(
                "model_node_final_answer_invalid_terminal_race_lost",
                extra={
                    "msg": "最终回答阶段无有效文本时 Run 已非运行态",
                    "data": {"step_id": step_id, "run_id": run_id},
                },
            )
        return {**end_state(step_count), "final_text": final_output}

    if finish_reason not in Constant.Workflow.NORMAL_FINISH_REASONS:
        # 已有文本不代表模型完成：例如 finish_reason=length 只说明本轮达到输出上限。
        # length 类截断不注入提示，直接回到模型节点由模型自行延续；缺失/未知完成原因才把
        # AIMessage 之后的 SystemMessage 作为下一模型步的显式续写指令。
        continuation_prompt = _build_continuation_prompt(finish_reason)
        if continuation_prompt is not None:
            task_space.defer_system_message(
                SystemMessage(content=continuation_prompt, additional_kwargs={"run_id": run_id})
            )
        log.warning(
            "model_node_output_requires_continuation",
            extra={
                "msg": "模型输出没有可接受的正常完成原因，回到模型节点继续",
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
            "next_node": ReactRoute.MODEL,
            "instruction": "",
        }

    # 如果存在系统修复提示，重新进入model
    if task_space.has_deferred_system_messages():
        return {
            "step_count": step_count,
            "next_node": ReactRoute.MODEL,
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
    return end_state(step_count)
