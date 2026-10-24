"""用户输入展示 parts 的构造与投影。"""

from collections.abc import Sequence

from app.assistant_transport.projection.snapshot_navigation import find_run, message
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import (
    ConversationStateFilePart,
    ConversationStateImagePart,
    ConversationStateTextPart,
)
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.constant import Constant
from app.core.events import UserInputConsumedEvent
from app.models.conversation_input import image_asset_id
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


def plan_user_input_consumed(
    event: UserInputConsumedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """把排队消息按身份搬进 Run，并补后续 assistant 气泡；重复投影按消息身份去重。

    排队条目已按展示绑定构造好 parts（actor 先刷新排队投影再投递，消费总在投影之后），这里只搬移
    不重建，事件因此只带消息身份。快照在消费提交后才懒重建时，消息已从 context 重建进 Run，按去重跳过。

    异常:
        KeyError: Run 不存在，或该消息既不在 Run 也不在排队列表——排队投影缺失，属契约错误。
    """

    # 领域身份是 inbox 行 id（int），wire 消息 id 是其字符串形式。
    message_id = str(event.message_id)
    run_index = find_run(state, event.run_id)
    messages = state["runs"][run_index]["messages"]
    if any(m["id"] == message_id for m in messages):
        return []
    queued = state.get("queued_messages", [])
    entry = next((m for m in queued if m["id"] == message_id), None)
    if entry is None:
        raise KeyError(f"queued message {message_id} not found")
    # 连续消费多条 user 时移除尚无输出的 assistant 占位。
    updated = list(messages)
    if updated and updated[-1]["role"] == "assistant" and not updated[-1]["parts"]:
        updated.pop()
    updated.extend(
        [
            message(message_id, "user", list(entry["parts"])),
            message(f"assistant-{message_id}", "assistant", []),
        ]
    )
    return [
        ConversationStateMutation("set", ("runs", run_index, "messages"), updated),
        ConversationStateMutation(
            "set", ("queued_messages",), [m for m in queued if m["id"] != message_id]
        ),
    ]
