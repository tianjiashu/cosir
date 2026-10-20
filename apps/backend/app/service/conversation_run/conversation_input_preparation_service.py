"""用户输入解析边界：文件校验、展示 token 和模型正文转换，不创建 Run。"""

import re
from pathlib import Path

from app.config.constant import Constant
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunFileAttachment,
)
from app.models.conversation_input import ConversationInput, QueuedTaskInput
from app.models.conversation_run_options import ConversationRunOptions, ReasoningEffort
from app.service.depends import get_model_config_service, get_task_crud
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


class ConversationInputPreparationService:
    """首条、追加及编辑消息共用的输入边界；Run 配置只作为入队发起意图返回。"""

    def __init__(self) -> None:
        self._task = get_task_crud()

    def prepare_input(
        self,
        task_id: int,
        command: ConversationRunCommand,
        model_config_id: int,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> QueuedTaskInput:
        """用户输入边界统一解析配置与附件，首条和后续消息共用。"""
        config = get_model_config_service().get_config(model_config_id)
        if reasoning_effort is not None and not config.supports_reasoning_effort:
            raise ValueError("reasoning_effort is not supported by the selected model")
        message = self._prepare_command(
            self._task.get(task_id).workspace_id,
            command,
            supports_image=config.supports_image,
        )
        return QueuedTaskInput(
            message=message,
            model_config_id=model_config_id,
            options=ConversationRunOptions(
                ban_tools=command.ban_tools, reasoning_effort=reasoning_effort
            ),
        )

    def _prepare_command(
        self,
        workspace_id: int | None,
        command: ConversationRunCommand,
        *,
        supports_image: bool,
    ) -> ConversationInput:
        """解析模型正文与展示附件；编辑路径已按消息身份补齐附件 locator。"""

        file_attachments = self._resolve_file_attachments(
            command.attachments,
            display_text=command.display_text,
        )
        input_text = self._resolve_file_tokens(command.display_text, file_attachments)

        if command.image_asset_ids and not supports_image:
            raise ValueError("model does not support image")
        image_paths = self._resolve_image_assets(
            workspace_id,
            command.image_asset_ids,
        )
        if not input_text.strip() and not image_paths:
            raise ValueError("input_text must be a non-empty string")
        return ConversationInput(
            input_text=input_text,
            image_paths=image_paths,
            display_text=command.display_text,
            attachments=file_attachments,
        )

    def _resolve_file_attachments(
        self,
        requested: list[ConversationRunAttachmentInput],
        *,
        display_text: str,
    ) -> list[ConversationRunFileAttachment]:
        """解析输入中的普通附件 token，不从 Run 猜测旧消息的附件归属。"""
        by_id = {attachment.id: attachment for attachment in requested}
        result: list[ConversationRunFileAttachment] = []
        # 编辑可携带原消息的全部绑定；只解析当前正文仍引用的附件。
        for attachment_id in dict.fromkeys(
            m.group(1) for m in find_message_tokens(Constant.Cosir.LOCAL_FILE_TOKEN, display_text)
        ):
            attachment = by_id.get(attachment_id)
            if attachment is None or attachment.path is None:
                raise ValueError("ordinary file attachment is unavailable")
            candidate = Path(attachment.path)
            if not (candidate.is_file() or candidate.is_dir()):
                raise ValueError("ordinary file attachment is unavailable")
            result.append(
                ConversationRunFileAttachment(
                    id=attachment.id,
                    name=attachment.name,
                    content_type=attachment.content_type,
                    path=attachment.path,
                )
            )
        return result

    @staticmethod
    def _resolve_file_tokens(
        text: str,
        attachments: list[ConversationRunFileAttachment],
    ) -> str:
        """将用户可见文本中的普通文件 token 替换为模型可读的本机路径。"""

        by_id = {attachment["id"]: attachment for attachment in attachments}

        def replace(match: re.Match[str]) -> str:
            return by_id[match.group(1)]["path"]

        return replace_message_tokens(
            Constant.Cosir.LOCAL_IMAGE_TOKEN,
            "",
            replace_message_tokens(Constant.Cosir.LOCAL_FILE_TOKEN, replace, text),
        )

    @staticmethod
    def _resolve_image_assets(
        workspace_id: int | None,
        image_asset_ids: list[str],
    ) -> list[str]:
        """校验本次图片聚合限制，并返回已上传图片的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        if workspace_id is None:
            raise ValueError("workspace_id is required when image attachments are present")
        from app.service.attachment.attachment_service import AttachmentService

        return AttachmentService().resolve_image_assets_for_run(workspace_id, image_asset_ids)
