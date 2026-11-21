"""工具观察写入 canonical context 后投影终态；不保存第二套调用生命周期。"""

from __future__ import annotations

from openai.types.chat import ChatCompletionToolMessageParam
from sqlalchemy.orm import Session as DbSession

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.execute.tool_terminal_projection import (
    terminal_event,
)
from app.core.tool.schemas import ToolObservation
from app.core.workflow.decision_expiry import (
    expire_decisions,
)
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.message_queue.session_message import (
    BackgroundResultBody,
    ToolImageBody,
)
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.models.message_metadata import ToolMessageMetadata


def settle_observations(batch: ToolBatch, *, step_id: str) -> None:
    """按观察顺序写 canonical tool 消息并投影终态；所有调用结果都保留，不以呈现方式过滤事实。

    这里的终态是持久化之后的权威终态。每个调用（含执行链内部异常收口的观察）已在 ``tools_node``
    里发过一次实时终态，两次都经 :func:`terminal_event` 由同一份观察构造，值必然相同，第二次在
    Transport 发布出口按空操作丢弃。tool 消息的 metadata 也取自同一个事件，持久化事实与实时投影因此同源。
    """
    runtime = _runtime_context()
    for observation in batch.observations:
        call_id = observation.tool_call_id
        event = terminal_event(
            observation,
            session_id=runtime.run.session_id,
            run_id=runtime.run.id,
            step_id=step_id,
            tool_call_id=call_id,
        )
        status = event.status
        canonical_write = _message_context().add_message(
            to_tool_model_message(observation), metadata=event.result
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
        emit(event)
        # 模型又问了一次（典型是停止后恢复）：同一 Session 至多一个等用户回应的等待，旧的作废。
        # 不另行告知模型——是它自己重新问的。
        if observation.awaiting_decision:
            expire_decisions(step_id=step_id, keep=frozenset({call_id}))


def settle_background_placeholder(
    tool_call_id: str, body: BackgroundResultBody, *, step_id: str, db: DbSession
) -> ToolCallStatusChangedEvent | None:
    """在认领结论的事务中结算原占位；返回事务提交后才可发布的卡片事件。"""
    runtime = _runtime_context()
    context = _message_context()
    current = context.tool_result_metadata(tool_call_id)
    if current is None or current.background is None or current.status != "pending":
        return None
    written = context.update_tool_result_metadata(
        tool_call_id,
        ToolMessageMetadata(
            status=body.placeholder_status,
            display_data=body.placeholder_display_data,
            error=body.placeholder_error,
            background=current.background.model_copy(update={"ended_at": body.ended_at}),
        ),
        db=db,
    )
    return ToolCallStatusChangedEvent(
        session_id=runtime.run.session_id,
        run_id=runtime.run.id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        result=written,
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


def withhold_unviewable_images(batch: ToolBatch) -> None:
    """本次 Run 的模型看不了图时，把观察带的图片换成正文里的一句说明。

    「图片只进能看图的模型」只在这里一处守住，所有工具一样：专门看图的（``read_image``、截图）与只是
    有时带图的（MCP）都照常执行，结果里有图再按本次 Run 的模型改写，工具不做判断。必须在写 tool 消息之前调用：说明
    要进 tool 消息正文，之后 ``write_tool_images`` 也就没有图可写。图片文件已存进附件目录，卡片照常显示。
    """
    with_images = [o for o in batch.observations if o.status == "success" and o.images]
    if not with_images or _runtime_context().agent.model.supports_image:
        return
    for observation in with_images:
        note = (
            f"[{len(observation.images)} image(s) returned by this tool are not shown: "
            "the current model cannot read images. Do not call tools just to look at images; "
            "use a text-only alternative instead]"
        )
        observation.content = f"{observation.content}\n{note}" if observation.content else note
        observation.images = ()


def write_tool_images(batch: ToolBatch) -> None:
    """把本批成功观察带的图片直接写进 context：每个调用一条带图的 user 消息。

    必须在整批 tool 消息写完之后调用：assistant 的 tool_calls 与对应 tool 消息之间不能插入 user
    消息。图片是调用结果的一部分，所以和 tool 消息一样在 observe 节点就地落库，不经 mailbox：经
    mailbox 时，同一批里有工具要求挂起（或 Run 被取消、步数用完）就会在 prepare_input 之前随执行
    丢弃，模型只剩一条「已附图片」的 tool 文本。
    """
    with_images = [o for o in batch.observations if o.status == "success" and o.images]
    if not with_images:
        return
    context = _message_context()
    for observation in with_images:
        message, metadata = ToolImageBody(
            tool_call_id=observation.tool_call_id,
            tool_name=observation.tool_name,
            image_paths=list(observation.images),
        ).render()
        context.add_message(message, metadata=metadata, project_to_transport=False)
