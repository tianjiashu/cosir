"""回合输入（用户输入、Agent 消息）展示 parts 的构造与消费投影。"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import (
    iter_parts,
    message,
    reply_index,
    track_turn,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateAgentMessagePart,
    ConversationStateImagePart,
    ConversationStateMessageSource,
    ConversationStateTextPart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.message_identity import reply_id, user_message_id
from app.core.events import InputConsumedEvent
from app.models.conversation_input import MessageSource, UserInputPresentation, image_asset_id

UserInputPart = ConversationStateTextPart | ConversationStateImagePart


def build_presentation_parts(presentation: UserInputPresentation) -> list[UserInputPart]:
    """按展示绑定构造用户消息 parts：一段胶囊正文，图片按发送顺序各一个稳定 locator。

    普通文件与工作区引用不再有独立 part：它们是正文里的 ``<file>`` / ``<ref>`` 胶囊。
    """

    parts: list[UserInputPart] = []
    if presentation.text:
        parts.append({"type": "text", "text": presentation.text, "status": "completed"})
    parts.extend(
        {"type": "image", "image": f"cosir-attachment://{image_asset_id(path)}"}
        for path in presentation.image_paths
    )
    return parts


def build_turn_input(
    presentation: UserInputPresentation, source: MessageSource | None
) -> tuple[list[UserInputPart], ConversationStateMessageSource | None]:
    """回合输入的 parts 与 wire 来源；消费投影与全量重建共用，两处展示一致。

    Agent 消息（``source`` 非空）是纯文本：不按胶囊投影——Agent 正文里恰好出现形如标签的文字时，
    按胶囊解析会改变展示，而它并不是用户选择的指针。
    """
    if source is None:
        return build_presentation_parts(presentation), None
    text: UserInputPart = {"type": "text", "text": presentation.text, "status": "completed"}
    return [text], {"taskId": source.task_id, "name": source.name}


def build_agent_message_part(
    message_id: int, presentation: UserInputPresentation, source: MessageSource
) -> ConversationStateAgentMessagePart:
    """途中到达的 Agent 消息；消费投影与全量重建共用。纯文本的理由同 ``build_turn_input``。"""
    return {
        "type": "agent-message",
        "id": user_message_id(message_id),
        "text": presentation.text,
        "source": {"taskId": source.task_id, "name": source.name},
    }


def plan_input_consumed(
    event: InputConsumedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """按事件自带的展示绑定把回合输入投影进快照，同时移出排队投影；按身份去重。

    不从排队投影里取内容：排队投影只镜像队列表，确认删除之后的任何一次刷新都会刷掉这一条。
    快照在消费提交后才懒重建时，消息已从 context 重建出来，按去重跳过。

    回合读事件上的回合号（写入时记下，冷重建读行上的同一个号）：这一回合已有回复，说明输入是途中
    来信，并进这条回复（写入规则保证途中来信时回复已存在）；还没有回复就是这条输入开启了回合，插入
    user 消息与这一回合的回复。由输入开启的回合总有回复（之后没有输出也保留空回复），冷重建同一
    规则，连续并入的输入因此各自成对，不用替换前一条的空回复。Agent 消息不进排队投影。
    """

    message_id = user_message_id(event.message_id)
    messages = state["messages"]
    if any(m["id"] == message_id for m in messages) or any(
        part["type"] == "agent-message" and part["id"] == message_id
        for _, part in iter_parts(state)
    ):
        return []
    tracked = track_turn(state, event.turn)
    existing = reply_index(state, event.turn)
    if existing is not None:
        return [
            *tracked,
            ConversationStateMutation(
                "set",
                ("messages", existing, "parts", len(messages[existing]["parts"])),
                # 不开回合的输入只会是途中来信（用户输入总开回合），必带来源。
                build_agent_message_part(
                    event.message_id, event.presentation, cast(MessageSource, event.source)
                ),
            ),
        ]
    queued = state.get("queued_messages", [])
    parts, source = build_turn_input(event.presentation, event.source)
    return [
        *tracked,
        ConversationStateMutation(
            "set",
            ("messages", len(messages)),
            message(message_id, "user", list(parts), turn=event.turn, source=source),
        ),
        ConversationStateMutation(
            "set",
            ("messages", len(messages) + 1),
            message(reply_id(event.turn), "assistant", [], turn=event.turn),
        ),
        ConversationStateMutation(
            "set", ("queued_messages",), [m for m in queued if m["id"] != message_id]
        ),
    ]
