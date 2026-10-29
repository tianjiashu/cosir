"""工具观察写入 canonical context 后投影终态；不保存第二套调用生命周期。"""

from __future__ import annotations


from openai.types.chat import ChatCompletionToolMessageParam

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.schemas import ToolObservation
from app.core.tool.execute.tool_terminal_projection import (
    terminal_event,
)
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.models.message_metadata import ToolMessageMetadata
from app.core.workflow.message_queue.task_message import ToolResultBody


def settle_observations(batch: ToolBatch, *, step_id: str) -> None:
    """按观察顺序写 canonical tool 消息并投影终态；可见性取本步创建事件的记账。

    这里的终态是持久化之后的权威终态。每个调用（含执行链内部异常收口的观察）已在 ``run_tool_batch``
    里发过一次实时终态，两次都经 :func:`terminal_event` 由同一份观察构造，值必然相同，消费方按自
    迁移幂等吸收。tool 消息的 metadata 也取自同一个事件，持久化事实与实时投影因此同源。
    """
    runtime = _runtime_context()
    for summary in batch.observations:
        call_id = summary["tool_call_id"]
        observation = ToolObservation(
            tool_name=summary["tool_name"],
            status=summary["status"],
            content=summary["content"],
            tool_call_id=call_id,
            display_data=summary.get("display_data"),
            team_run_id=summary.get("team_run_id"),
            awaiting_decision=summary.get("awaiting_decision", False),
            deferred=summary.get("deferred", False),
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
                deferred=observation.deferred,
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


def settle_decision_observation(batch: ToolBatch, *, step_id: str) -> None:
    """结算用户批准后 HITL 调用的执行结果：结果追加为一条 user 消息，等待占位只更新展示状态。

    占位在模型协议上已是这次调用的结果（见 ``tool_awaiting_decision``），决定可能在占位之后的若干
    模型步、甚至之后的 Run 才到，此时原调用早已不是最后一批，不能再写 tool 消息；改写历史行又会
    让后续请求前缀分叉。所以模型从末尾追加的 ``<tool_result>`` 读到结果，卡片从占位行的 metadata
    读到终态。
    """
    runtime = _runtime_context()
    context = _message_context()
    [summary] = batch.observations
    call_id = summary["tool_call_id"]
    observation = ToolObservation(
        tool_name=summary["tool_name"],
        status=summary["status"],
        content=summary["content"],
        tool_call_id=call_id,
        display_data=summary.get("display_data"),
        team_run_id=summary.get("team_run_id"),
    )
    message, metadata = ToolResultBody(
        tool_call_id=call_id,
        tool_name=observation.tool_name,
        status=observation.status,
        content=str(to_tool_model_message(observation)["content"]),
    ).render()
    context.add_message(message, metadata=metadata, project_to_transport=False)
    event = terminal_event(
        observation,
        task_id=runtime.run.task_id,
        run_id=runtime.run.id,
        step_id=step_id,
        tool_call_id=call_id,
    )
    written = context.update_tool_result_metadata(
        call_id,
        ToolMessageMetadata(
            status=event.status,
            display_data=event.display_data,
            error=event.error,
            team_run_id=event.team_run_id,
        ),
    )
    log.info(
        "tool_decision_result_persisted",
        extra={
            "msg": "用户决定后的工具结果已追加为消息，占位只更新展示状态",
            "data": {
                "run_id": runtime.run.id,
                "tool_call_id": call_id,
                "status": event.status,
            },
        },
    )
    if not written.hidden:
        # 卡片按实际写入的 metadata 渲染：结果没带展示数据时沿用了占位的。
        emit(
            event.model_copy(
                update={"display_data": written.display_data, "team_run_id": written.team_run_id}
            )
        )


def settle_deferred_placeholder(tool_call_id: str, *, step_id: str) -> None:
    """后台结论并入 context 后，把这次调用的后台占位推到 ``completed``；只更新展示状态。

    在并入时而不是 ``deferred_registry.complete`` 时结算：工作可能早于占位行写入就完成，并入发生在
    之后的模型步，占位行一定已写入。已被撤销或重启改为 ``cancelled`` 的占位照样推到 ``completed``
    （结论确实到了，投影白名单允许 ``cancelled → completed``）；不是后台占位或已完成的不动。
    """
    runtime = _runtime_context()
    context = _message_context()
    current = context.tool_result_metadata(tool_call_id)
    if current is None or not current.deferred or current.status == "completed":
        return
    written = context.update_tool_result_metadata(
        tool_call_id, ToolMessageMetadata(status="completed")
    )
    log.info(
        "deferred_placeholder_completed",
        extra={
            "msg": "后台结论已并入，占位只更新展示状态",
            "data": {"run_id": runtime.run.id, "tool_call_id": tool_call_id},
        },
    )
    if not written.hidden:
        emit(
            ToolCallStatusChangedEvent(
                task_id=runtime.run.task_id,
                run_id=runtime.run.id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                status="completed",
                display_data=written.display_data,
                team_run_id=written.team_run_id,
            )
        )


def to_tool_model_message(observation: ToolObservation) -> ChatCompletionToolMessageParam:
    """把工具观察转为模型可见的 ``role="tool"`` 消息，闭合 ``assistant.tool_calls`` 配对。

    失败与取消文本已由 :func:`tool_error` / :func:`tool_cancelled` 在源头组织好，这里原样转交；
    只有无正文的成功调用补一个 ``success``。执行状态留在 observation 与 Transport metadata，
    不进入模型消息。
    """
    content = observation.content.strip() if isinstance(observation.content, str) else ""
    return {
        "role": "tool",
        "content": content or observation.status,
        "tool_call_id": observation.tool_call_id,
    }
