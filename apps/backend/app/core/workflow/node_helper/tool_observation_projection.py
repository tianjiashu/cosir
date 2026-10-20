"""工具观察写入 canonical context 后投影终态；不保存第二套调用生命周期。"""

from __future__ import annotations

from typing import Any

from app.assistant_transport.event import ToolCallStatusChangedEvent
from app.config.logging.logger import log
from app.core.context.tool_call_closure import build_waiting_input_tool_message
from app.core.tools.schemas import USER_INPUT_REQUEST_KEY, ToolObservation, UserInputRequest
from app.core.tools.tool_execute.tool_terminal_projection import (
    normalize_display_data,
    terminal_error_hint,
    terminal_status,
)
from app.core.workflow.node_helper.common import (
    _message_context,
    _runtime_context,
    get_stream_writer,
)
from app.models.message_metadata import ToolMessageMetadata


def project_waiting_request(summary: dict[str, Any], *, step_id: str) -> None:
    """挂起前物化审批卡片与占位结果，重启后可从 context 重建同一表单。"""
    runtime = _runtime_context()
    call_id = summary["tool_call_id"]
    if call_id not in runtime.step_context.projected_tool_calls:
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
    get_stream_writer()(
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
    """按观察顺序闭合模型协议并计算连续失败；可见性取本步创建事件的记账。"""
    runtime = _runtime_context()
    count = inherited_error_count
    errors = 0
    for summary in summaries:
        call_id = summary["tool_call_id"]
        status = terminal_status(summary["status"])
        display_data = normalize_display_data(summary.get("display_data"), tool_call_id=call_id)
        hint = terminal_error_hint(status, summary.get("display_data"))
        observation = ToolObservation(
            tool_name=summary["tool_name"],
            status=summary["status"],
            content=summary["content"],
            error=summary["error"],
            reason=summary["reason"],
            retryable=summary["retryable"],
            tool_call_id=call_id,
            display_data=display_data,
            team_run_id=summary.get("team_run_id"),
        )
        visible = call_id in runtime.step_context.projected_tool_calls
        canonical_write = _message_context().add_message(
            runtime.operations.to_tool_model_message(observation),
            metadata=ToolMessageMetadata(
                status=status,
                display_data=display_data,
                error=hint,
                team_run_id=observation.team_run_id,
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
            get_stream_writer()(
                ToolCallStatusChangedEvent(
                    task_id=runtime.run.task_id,
                    run_id=runtime.run.id,
                    step_id=step_id,
                    tool_call_id=call_id,
                    status=status,
                    error=hint,
                    display_data=display_data,
                    team_run_id=observation.team_run_id,
                )
            )
        if status == "completed":
            count = 0
        elif status == "failed" and summary["retryable"] is False:
            count += 1
            errors += 1
    return count, errors
