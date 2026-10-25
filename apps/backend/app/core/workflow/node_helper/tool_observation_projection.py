"""工具观察写入 canonical context 后投影终态；不保存第二套调用生命周期。"""

from __future__ import annotations


from openai.types.chat import ChatCompletionToolMessageParam

from app.config.logging.logger import log
from app.core.tool.schemas import ToolObservation
from app.core.tool.execute.tool_terminal_projection import (
    terminal_event,
)
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.models.tool_batch import ToolBatch
from app.models.message_metadata import ToolMessageMetadata


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
