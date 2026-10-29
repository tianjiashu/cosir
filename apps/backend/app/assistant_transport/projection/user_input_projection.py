"""用户输入展示 parts 的构造与投影。"""

from collections.abc import Sequence
from typing import cast

from app.assistant_transport.projection.snapshot_navigation import message
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateFilePart,
    ConversationStateImagePart,
    ConversationStateTextPart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.assistant_transport.state.message_identity import reply_id, user_message_id
from app.config.constant import Constant
from app.core.events import UserInputConsumedEvent
from app.models.conversation_input import UserInputPresentation, image_asset_id
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment
from app.utils.message_tokens import find_message_tokens

UserInputPart = ConversationStateTextPart | ConversationStateImagePart | ConversationStateFilePart


def build_user_input_parts(
    display_text: str,
    image_paths: Sequence[str],
    file_attachments: Sequence[ConversationRunFileAttachment],
) -> list[UserInputPart]:
    """将已消费或排队输入的展示绑定投影为安全 locator parts，不读取附件内容。

    display_text 中的 token 保留编辑器顺序；未嵌入正文的图片按输入顺序追加。
    token 无对应附件或输入无可展示内容时抛 ValueError。
    """

    attachments_by_id: dict[str, ConversationRunFileAttachment] = {}
    for attachment_record in file_attachments:
        attachment_id = attachment_record.get("id")
        if (
            not attachment_id
            or not Constant.Transport.FILE_LOCATOR.fullmatch(f"cosir-local-file:{attachment_id}")
            or not attachment_record.get("name")
            or not attachment_record.get("content_type")
        ):
            raise ValueError("ordinary file attachment is malformed")
        attachments_by_id[attachment_id] = attachment_record

    image_paths_by_id: dict[str, str] = {}
    for path in image_paths:
        image_id = image_asset_id(path)
        locator = f"cosir-attachment://{image_id}"
        if not Constant.Transport.IMAGE_LOCATOR.fullmatch(locator):
            raise ValueError("image attachment locator is invalid")
        image_paths_by_id[image_id] = path

    parts: list[UserInputPart] = []
    cursor = 0
    referenced_image_ids: set[str] = set()
    referenced_file_ids: set[str] = set()
    for match in find_message_tokens(Constant.Cosir.LOCAL_INPUT_TOKEN, display_text):
        text_part = display_text[cursor : match.start()]
        if text_part:
            parts.append({"type": "text", "text": text_part, "status": "completed"})
        marker_kind, token_id = match.groups()
        if marker_kind == "file":
            attachment = attachments_by_id.get(token_id)
            if attachment is None:
                raise ValueError("ordinary file attachment is unavailable")
            if token_id in referenced_file_ids:
                cursor = match.end()
                continue
            parts.append(
                {
                    "type": "file",
                    "file": f"cosir-local-file:{attachment['id']}",
                    "name": attachment["name"],
                    "contentType": attachment["content_type"],
                }
            )
            referenced_file_ids.add(token_id)
        else:
            if token_id not in image_paths_by_id:
                raise ValueError("image attachment is unavailable")
            if token_id in referenced_image_ids:
                cursor = match.end()
                continue
            parts.append({"type": "image", "image": f"cosir-attachment://{token_id}"})
            referenced_image_ids.add(token_id)
        cursor = match.end()
    remaining_text = display_text[cursor:]
    if remaining_text:
        parts.append({"type": "text", "text": remaining_text, "status": "completed"})
    parts.extend(
        {"type": "image", "image": f"cosir-attachment://{image_id}"}
        for image_id in image_paths_by_id
        if image_id not in referenced_image_ids
    )
    if not parts:
        raise ValueError("user input event must contain at least one part")
    return parts


def build_presentation_parts(presentation: UserInputPresentation) -> list[UserInputPart]:
    """按展示绑定构造用户消息 parts；排队投影、消费投影与全量重建共用，三处展示一致。"""
    return build_user_input_parts(
        presentation.display_text, presentation.image_paths, presentation.attachments
    )


def plan_user_input_consumed(
    event: UserInputConsumedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """按事件自带的展示绑定插入 user 消息并补其后的助手占位，同时移出排队投影；按身份去重。

    不从排队投影里取内容：排队投影只镜像队列表，确认删除之后的任何一次刷新都会刷掉这一条。
    快照在消费提交后才懒重建时，消息已从 context 重建出来，按去重跳过。连续消费多条 user 时，
    同一 Run 尚无输出的助手占位被这条 user 消息原位替换。
    """

    message_id = user_message_id(event.message_id)
    # 消费只发生在某个 Run 的 prepare_input 节点内，事件总带 Run 身份。
    run_id = cast(int, event.run_id)
    messages = state["messages"]
    if any(m["id"] == message_id for m in messages):
        return []
    insert_at = len(messages)
    last = messages[-1] if messages else None
    if (
        last is not None
        and last["role"] == "assistant"
        and last["runId"] == run_id
        and not last["parts"]
    ):
        insert_at -= 1
    queued = state.get("queued_messages", [])
    return [
        ConversationStateMutation(
            "set",
            ("messages", insert_at),
            message(message_id, "user", list(build_presentation_parts(event.presentation)), run_id),
        ),
        ConversationStateMutation(
            "set",
            ("messages", insert_at + 1),
            message(reply_id(event.message_id), "assistant", [], run_id),
        ),
        ConversationStateMutation(
            "set", ("queued_messages",), [m for m in queued if m["id"] != message_id]
        ),
    ]
