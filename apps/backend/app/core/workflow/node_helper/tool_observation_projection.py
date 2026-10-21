"""工具观察写入 canonical context 后投影终态；不保存第二套调用生命周期。"""

from __future__ import annotations

from typing import Any

from openai.types.chat import ChatCompletionToolMessageParam

from app.config.logging.logger import log
from app.core.context.tool_call_closure import build_waiting_input_tool_message
from app.core.events import ToolCallStatusChangedEvent
from app.core.tools.schemas import USER_INPUT_REQUEST_KEY, ToolObservation, UserInputRequest
from app.core.tools.tool_execute.tool_terminal_projection import (
    normalize_display_data,
    terminal_event,
)
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.models.message_metadata import ToolMessageMetadata


def project_waiting_request(summary: dict[str, Any], *, step_id: str) -> None:
    """挂起前物化审批卡片与占位结果，重启后可从 context 重建同一表单。"""
    runtime = _runtime_context()
    batch = runtime.batch
    call_id = summary["tool_call_id"]
    if not batch.was_projected(call_id):
        raise ValueError(f"待决调用没有可供用户作答的工具卡片：{call_id}")
    request = UserInputRequest.from_dict(summary[USER_INPUT_REQUEST_KEY])
    display_data = dict(
        normalize_display_data(summary.get("display_data"), tool_call_id=call_id) or {}
    )
    display_data[USER_INPUT_REQUEST_KEY] = request.to_request_payload()
    canonical_write = _message_context().add_message(
        build_waiting_input_tool_message(call_id, summary["tool_name"]),
        metadata=ToolMessageMetadata(status="running", display_data=display_data),
        run_id=runtime.run.id,
    )
    log.info(
        "user_input_request_transport_persisted",
        extra={
            "msg": "待决请求已投影并持久化",
            "data": {
                "run_id": runtime.run.id,
                "tool_call_id": call_id,
                "request_id": request.request_id,
                "canonical_write": canonical_write,
            },
        },
    )
    emit(
        ToolCallStatusChangedEvent(
            task_id=runtime.run.task_id,
            run_id=runtime.run.id,
            step_id=step_id,
            tool_call_id=call_id,
            status="running",
            display_data=display_data,
        )
    )


def settle_observations(
    summaries: list[dict[str, Any]], *, step_id: str, inherited_error_count: int
) -> tuple[int, int]:
    """按观察顺序写 canonical tool 消息、投影终态并计算连续失败；可见性取本步创建事件的记账。

    这里的终态是持久化之后的权威终态。执行过的调用已在 ``run_tool_batch`` 结束时发过一次实时
    终态，两次都经 :func:`terminal_event` 由同一份观察构造，值必然相同，消费方按自迁移幂等吸收；
    未经执行器的观察（被拦截的批次、被驳回的调用）只有这一次。tool 消息的 metadata 也取自同一个
    事件，持久化事实与实时投影因此同源。
    """
    runtime = _runtime_context()
    batch = runtime.batch
    count = inherited_error_count
    errors = 0
    for summary in summaries:
        call_id = summary["tool_call_id"]
        observation = ToolObservation(
            tool_name=summary["tool_name"],
            status=summary["status"],
            content=summary["content"],
            error=summary["error"],
            reason=summary["reason"],
            retryable=summary["retryable"],
            tool_call_id=call_id,
            display_data=summary.get("display_data"),
            team_run_id=summary.get("team_run_id"),
        )
        event = terminal_event(
            observation,
            task_id=runtime.run.task_id,
            run_id=runtime.run.id,
            step_id=step_id,
            tool_call_id=call_id,
        )
        status = event.status
        visible = batch.was_projected(call_id)
        canonical_write = _message_context().add_message(
            to_tool_model_message(observation),
            metadata=ToolMessageMetadata(
                status=status,
                display_data=event.display_data,
                error=event.error,
                team_run_id=event.team_run_id,
                hidden=not visible,
            ),
        )
        log.info(
            "tool_observation_persisted",
            extra={
                "msg": "工具观察已写入 canonical context",
                "data": {
                    "run_id": runtime.run.id,
                    "tool_call_id": call_id,
                    "status": status,
                    "canonical_write": canonical_write,
                },
            },
        )
        if visible:
            emit(event)
        if status == "completed":
            count = 0
        elif status == "failed" and summary["retryable"] is False:
            count += 1
            errors += 1
    return count, errors


def to_tool_model_message(observation: ToolObservation) -> ChatCompletionToolMessageParam:
    """把工具观察压缩为模型可见的 ``role="tool"`` 消息，闭合 ``assistant.tool_calls`` 配对。

    三类终态使用不同的最小契约：``success`` 只发内容（没有内容时发 ``"success"``）；``error``
    发错误、是否可重试的提示与 ``reason``，不重复发 ``content``（失败观察的 ``content`` 通常
    就是错误副本）；``cancelled`` 只发取消说明，不伪装成可重试错误。执行状态留在 observation
    与 Transport metadata，不进入模型消息。
    """
    content = observation.content.strip() if isinstance(observation.content, str) else ""
    error = observation.error.strip() if isinstance(observation.error, str) else ""
    reason = observation.reason.strip() if isinstance(observation.reason, str) else ""

    if observation.status == "success":
        content_text = content or "success"
    elif observation.status == "cancelled":
        content_text = f"cancelled: {reason}" if reason else "cancelled"
    else:
        lines = [f"error: {error or 'the tool call failed'}"]
        if observation.retryable:
            lines.append(
                "hint: this error can be retried; decide from the context whether "
                "retrying is appropriate."
            )
        else:
            lines.append("hint: do not retry this tool call.")
        if reason:
            lines.append(f"reason: {reason}")
        content_text = "\n".join(lines)

    return {"role": "tool", "content": content_text, "tool_call_id": observation.tool_call_id}
