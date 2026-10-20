"""模型步骤：流式正文、思考、工具调用与用量归一，按完成原因选择下一步。"""

import asyncio
from contextlib import aclosing
from typing import cast

from openai.lib.streaming.chat import ParsedChatCompletionSnapshot
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ParsedFunctionToolCall,
)

from app.config.logging.logger import log
from app.core.llm_provider.message_codec import assistant_message
from app.core.llm_provider.openai_model import usage_from_response
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.node_helper.common import (
    RunStopped,
    _message_context,
    _runtime_context,
    route_state,
)
from app.core.workflow.node_helper.finalize_max_steps import _finalize_max_steps
from app.core.workflow.node_helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.models.runtime_notice import RuntimeNotice
from app.utils.message_content import content_to_text


async def _model_node(state: ReactExecutionState) -> dict[str, object]:
    """调用 Run 独占的 SDK 模型，逐帧替换持久化响应，结束后纳入上下文。
    取消覆盖首字节等待及流式读取：部分输出留作展示，不进入模型上下文或工具执行。
    工具调用只按 id 与注册名过滤，参数交 tools；无保留调用时，完成原因落定 Run。
    无保留调用且缺少完成原因时，输出仅供展示并重新请求模型。"""

    rc = _runtime_context()
    operations = rc.operations
    model = rc.model

    step_count = state.step_count + 1
    task_id = operations.get_current_task().id
    run_id = operations.get_current_run().id
    step_id = f"step-{step_count}"

    # 超配额时不再发起推理，落定 max_steps_reached 失败终态。
    if step_count > rc.step_limit:
        return await _finalize_max_steps(step_count)
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    # load_message() 出口已归一化 assistant 消息，此处直接取用，不再重复 sanitize。
    task_space = task_runtime_spaces.get_or_create(task_id)
    # 运行期通知（其它层经 tools / runner / 配置变更广播投递）统一在这里消费：归属过滤由
    # ``take_runtime_notices(run_id=...)`` 在出队时完成（属于其它 Run 的通知在此被丢弃），此处不再
    # 重复判定。取回的是压成一段的提示文本（队列为空时也含声明行），作为独立 ``system 消息``
    # 落库进 canonical context——它成为历史固定行后，后续 Run 的请求前缀才不会在此分叉，因此必须
    # 重新 ``load_message()`` 才能让本步请求带上它。
    notice = task_space.take_runtime_notices(run_id=run_id)
    if notice:
        _message_context().add_message(
            ChatCompletionSystemMessageParam(role="system", content=notice)
        )
    # load_message 含附件读取，必须离开事件循环。
    messages: list[ChatCompletionMessageParam] = await asyncio.to_thread(
        _message_context().load_message
    )
    registered_tool_names = frozenset(definition.name for definition in operations.all_vaild_tools)

    if operations.is_current_run_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 run 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": run_id},
            },
        )
        operations.cancel_run_if_running(usage_stats=rc.usage_stats)
        raise RunStopped()

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
    # 每次模型请求新建批次，取消只收敛本次已投影的调用。
    rc.batch = ToolBatch()
    parts = StreamingPartStateMachine(task_id=task_id, run_id=run_id, step_id=step_id)

    # SDK 快照负责聚合；context 只保存请求正文副本和独立响应元数据。
    aggregated_message: ParsedChatCompletionSnapshot | None = None

    try:
        async with aclosing(model.stream(messages)) as chunks:
            async for event in chunks:
                if operations.is_current_run_cancelled():
                    raise RunStopped()
                aggregated_message = event.snapshot
                _message_context().save_model_response(
                    aggregated_message,
                    include_in_context=False,
                    registered_tool_names=registered_tool_names,
                )

                delta = event.chunk.choices[0].delta if event.chunk.choices else None
                reasoning = (
                    (delta.model_extra or {}).get(rc.thinking_channel)
                    if delta is not None and rc.thinking_channel
                    else None
                )
                # 工具预览使用完整快照里的调用身份；参数只在流结束后解析一次。
                raw_tool_calls: list[ParsedFunctionToolCall] = (
                    aggregated_message.choices[0].message.tool_calls or []
                )
                # 保留通道原始分片，换行和空白也是模型输出的一部分。
                if delta and delta.content:
                    parts.text(delta.content)
                if reasoning:
                    parts.reasoning(reasoning)
                if raw_tool_calls:
                    # 一个 chunk 可能并行携带多个 tool call，逐条处理已有的 name/id 身份。
                    parts.tool_call(raw_tool_calls)
            # 最后一帧之后仍可能收到取消信号，进入完成判定前再检查一次。
            if operations.is_current_run_cancelled():
                raise RunStopped()
    except RunStopped:
        log.info(
            "model_node_cancelled_usage_summary",
            extra={
                "msg": "模型调用已关闭，保存中断输出并收口工具预览",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "step_id": step_id,
                    "usage": rc.usage_stats.to_dict(),
                },
            },
        )
        # 取消时工具 part 的收敛由状态机按「本请求已投影的调用」完成，不再查询生命周期记录。
        parts.cancel()
        operations.cancel_run_if_running(usage_stats=rc.usage_stats)
        raise
    finally:
        parts.finish()

    # 流关闭后有保留调用或完成原因才提交上下文；其余草稿只保留展示行。
    if aggregated_message is None:
        raise RuntimeError("模型流没有产出响应")
    choice = aggregated_message.choices[0]
    # SDK 快照的静态类型只列完整原因，流被截断时运行时仍可能为 None。
    finish_reason = cast(str | None, choice.finish_reason)
    ai_message: ChatCompletionAssistantMessageParam = assistant_message(
        choice.message, registered_tool_names
    )
    calls = [
        cast(ChatCompletionMessageFunctionToolCallParam, call)
        for call in ai_message.get("tool_calls") or []
    ]
    if calls:
        # 最终消息是有效调用的权威集合；补记最后一帧才完整的身份。
        parts.tool_call(choice.message.tool_calls or [])

    if not calls and finish_reason is None:
        parts.cancel()
        _message_context().abandon_model_response()
        task_space.defer_runtime_notice(
            RuntimeNotice(content="请继续未完成的任务", kind="model_continuation", run_id=run_id)
        )
        return route_state(step_count, ReactRoute.MODEL)

    _message_context().save_model_response(
        aggregated_message,
        include_in_context=True,
        registered_tool_names=registered_tool_names,
    )

    usage = usage_from_response(aggregated_message.usage)
    rc.usage_stats.add_usage_metadata(usage)
    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "output_text_length": len(content_to_text(ai_message.get("content"))),
                "finish_reason": finish_reason,
            },
        },
    )

    if calls:
        # 有任意有效工具调用都进 tools 节点；参数是否合法由执行边界决定，失败由 observe 结算；
        # 工具结果是否回流 model 也由 observe 决定。``instruction`` 把模型同时给出的说明文本带给
        # 下游，使工具执行与错误排查能看到模型当时的意图。
        rc.batch.calls = calls
        content = ai_message.get("content")
        rc.batch.instruction = content if isinstance(content, str) else ""
        return route_state(step_count, ReactRoute.TOOLS)

    # 无工具调用且 finish_reason 非 None：正文可以为空；结构化处理作为已完成 Run 的支线。
    final_answer = content_to_text(ai_message.get("content"))
    rc.batch = ToolBatch()
    structured = rc.structured_output is not None
    log.info(
        "model_node_final_response",
        extra={
            "msg": f"模型给出最终回复，即将落库，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "output_text_length": len(final_answer),
                "finish_reason": finish_reason,
                "structured_output_pending": structured,
            },
        },
    )
    completed = operations.complete_run_if_running(
        rc.usage_stats, final_output=final_answer, end_reason=finish_reason
    )
    return route_state(
        step_count,
        ReactRoute.STRUCTURED_OUTPUT if structured and completed else ReactRoute.END,
    )
