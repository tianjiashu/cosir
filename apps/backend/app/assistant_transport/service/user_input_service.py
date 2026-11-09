"""用户输入解析边界：模型能力校验、附件、图片与 Skill 解析、展示 token 转模型正文，产出待入队的
``TaskMessage``。

HTTP 发送使用；Agent 之间的输入走 ``agent_message``。只读
Task、模型配置与已发布图片的元数据，不读写普通附件文件、不写库、不入队、不创建 Run。投递方在入队
之前调用（解析 IO 不占用 Task 闸门），输入不合法在这里同步拒绝，不让坏数据进外部队列。
"""

import re
from pathlib import Path
from urllib.parse import unquote

from app.config.constant import Constant
from app.core.workflow.message_queue.task_message import (
    Delivery,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunFileAttachment,
)
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions, ReasoningEffort
from app.models.errors.input_errors import UserInputRejectedError
from app.service.attachment.attachment_service import AttachmentService
from app.core.workflow.message_queue.message_bodies.rendering import escape_tag
from app.service.configuration.skill_catalog import SkillCatalog, SkillLoadError
from app.service.configuration.skill_configuration import SkillConfigurationError
from app.service.depends import get_model_config_service, get_task_crud, get_workspace_service
from app.utils.message_tokens import find_message_tokens, replace_message_tokens

_SKILL_TAG = "skill"


class UserInputService:
    """进程级单例（``service.depends.get_user_input_service``），无进程内状态。"""

    def __init__(self) -> None:
        self._task = get_task_crud()

    def prepare(
            self,
            task_id: int,
            command: ConversationRunCommand,
            model_config_id: int,
            reasoning_effort: ReasoningEffort | None = None,
            agent_id: str = "main_agent",
            *,
            delivery: Delivery = "immediate",
    ) -> TaskMessage:
        """校验模型能力、解析附件、图片与 Skill，组装成携带发起配置的用户输入消息（尚未入队、没有行 id）。

        ``delivery`` 是信封属性，原样带上。普通附件的路径一律由请求携带：撤回后放回输入框的附件，
        路径来自快照里的展示绑定（``build_user_input_parts``），这里不从历史消息恢复。

        异常:
            UserInputRejectedError: 模型配置不存在、模型不支持所选能力、附件 / 图片 / Skill 不可用、输入为空。
            ImageNormalizationError: 本次图片数量或总大小超过统一输入限制。
            KeyError: Task 不存在（接入层映射为 404）。
        """
        task = self._task.get(task_id)
        config = get_model_config_service().check_launch_config(
            model_config_id, reasoning_effort
        )
        if command.image_asset_ids and not config.supports_image:
            raise UserInputRejectedError(
                "IMAGE_NOT_SUPPORTED", "当前模型不支持图片输入，请更换模型或移除图片"
            )
        self._check_image_token_bindings(command)
        file_attachments = self._resolve_file_attachments(
            command.attachments,
            display_text=command.display_text,
        )
        skills = self._resolve_skills(task.workspace_id, command.display_text)
        input_text = self._resolve_input_tokens(command.display_text, file_attachments) + "".join(
            f'\n\n<{_SKILL_TAG} name="{name}">\n{escape_tag(text, _SKILL_TAG)}\n</{_SKILL_TAG}>'
            for name, text in skills.items()
        )
        image_paths = self._resolve_image_assets(task.workspace_id, command.image_asset_ids)
        # wire schema 已保证文本 part 非空，但「正文只剩图片 token」时替换后同样是空。
        if not input_text.strip() and not image_paths:
            raise UserInputRejectedError("EMPTY_INPUT", "请输入消息内容")
        body = UserInputBody(
            input_text=input_text,
            presentation=UserInputPresentation(
                display_text=command.display_text,
                image_paths=image_paths,
                attachments=file_attachments,
            ),
            launch=RunLaunch(
                agent_id=agent_id,
                model_config_id=model_config_id,
                options=ConversationRunOptions(
                    disabled_tool_groups=command.disabled_tool_groups,
                    reasoning_effort=reasoning_effort,
                ),
            ),
        )
        return TaskMessage.of(body, delivery=delivery)

    @staticmethod
    def _check_image_token_bindings(command: ConversationRunCommand) -> None:
        """正文里的图片 token 必须绑定本次请求上传的图片资源。

        图片正文不进模型文本（由 image content part 承载），漏绑不会在正文里露馅；但展示投影
        （``build_user_input_parts``）要求每个图片 token 都能解析成图片资源。少了这条，用户随手
        输入一段形如 token 的文本就会在入队后的快照重建里抛错，把 Task 停在「有 running Run
        却没有执行者」的状态。
        """
        assets = set(command.image_asset_ids)
        unbound = [
            token_id
            for token_id in dict.fromkeys(
                match.group(1)
                for match in find_message_tokens(
                    Constant.Cosir.LOCAL_IMAGE_TOKEN, command.display_text
                )
            )
            if token_id not in assets
        ]
        if unbound:
            raise UserInputRejectedError(
                "IMAGE_ATTACHMENT_UNAVAILABLE",
                "正文中的图片已失效，请重新添加图片",
                detail=f"image token(s) {unbound} have no uploaded asset",
            )

    @staticmethod
    def _resolve_file_attachments(
            requested: list[ConversationRunAttachmentInput],
            *,
            display_text: str,
    ) -> list[ConversationRunFileAttachment]:
        """把正文引用的普通附件 token 绑定到本次请求（或复用）的附件描述符。

        只做绑定，不访问文件系统：本机 Agent 的附件路径来自前端原生选择器，存在性由真正读取
        它的工具负责。这一层提前 stat 既保证不了后续读取（TOCTOU），又会把权限不足、网络盘卡顿
        误报成「附件不可用」，而展示绑定只需要一条稳定的引用。
        """
        by_id = {attachment.id: attachment for attachment in requested}
        result: list[ConversationRunFileAttachment] = []
        # 请求可能带着正文已删掉引用的附件；只解析当前正文仍引用的。
        for attachment_id in dict.fromkeys(
                m.group(1) for m in find_message_tokens(Constant.Cosir.LOCAL_FILE_TOKEN, display_text)
        ):
            attachment = by_id.get(attachment_id)
            if attachment is None:
                raise UserInputRejectedError(
                    "ATTACHMENT_UNAVAILABLE",
                    "附件已失效，请重新选择附件",
                    detail=f"ordinary file attachment {attachment_id} is unavailable (no request attachment)",
                )
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
    def _resolve_skills(workspace_id: int, display_text: str) -> dict[str, str]:
        """正文里选中的 Skill（按首次出现去重）→ 交给模型的正文。

        正文在入队时固化：之后改动或删除 Skill 不影响这条已发出的输入，历史可复现、上下文前缀也
        不会因为 Skill 文件变化而漂移。名称按 Task 所在工作区解析（工作区同名覆盖系统级），不存在或
        已停用时同步拒绝，前端收回乐观消息并恢复草稿。
        """

        names = list(
            dict.fromkeys(
                unquote(match.group(1))
                for match in find_message_tokens(Constant.Cosir.LOCAL_SKILL_TOKEN, display_text)
            )
        )
        if not names:
            return {}
        workspace_root = Path(get_workspace_service().get_workspace(workspace_id).root_path)
        catalog = SkillCatalog.visible_from(workspace_root)
        documents: dict[str, str] = {}
        for name in names:
            try:
                documents[name] = catalog.load_enabled(name).for_model()
            except (SkillLoadError, SkillConfigurationError) as exc:
                raise UserInputRejectedError(
                    "SKILL_UNAVAILABLE",
                    f"Skill「{name}」不可用，请重新选择",
                    detail=f"skill {name!r} cannot be loaded: {exc}",
                ) from exc
        return documents

    @staticmethod
    def _resolve_input_tokens(
            text: str,
            attachments: list[ConversationRunFileAttachment],
    ) -> str:
        """把正文里的 token 换成模型可读内容：文件换路径，图片由 image part 承载、正文丢弃，Skill 换成
        名称引用（正文另附在输入末尾）。

        必须单趟扫描三类 token：分两趟时第一趟插入的文件路径会被第二趟当成原文再解析，路径里含
        token 或 Markdown 围栏就会把正文改坏。
        """
        paths = {attachment["id"]: attachment["path"] for attachment in attachments}

        def substitute(match: re.Match[str]) -> str:
            kind, token_id = match.group(1), match.group(2)
            if kind == "file":
                return paths[token_id]
            if kind == "skill":
                return f'[Skill "{unquote(token_id)}"]'
            return ""

        return replace_message_tokens(Constant.Cosir.LOCAL_INPUT_TOKEN, substitute, text)

    @staticmethod
    def _resolve_image_assets(workspace_id: int, image_asset_ids: list[str]) -> list[str]:
        """校验本次图片聚合限制，并返回已上传图片的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        return AttachmentService().resolve_image_assets_for_run(workspace_id, image_asset_ids)
