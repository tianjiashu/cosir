"""用户输入解析边界：模型能力校验、附件与图片解析、展示 token 转模型正文，产出待入队的
``TaskMessage``。

HTTP 发送、子 Agent 委派与追加、Team 节点输入共用，编辑取返回消息的 ``body``；只读 Task、模型
配置与已发布图片的元数据，不读写普通附件文件、不写库、不入队、不创建 Run。投递方在入队之前调用
（解析 IO 不占用 Task 闸门），输入不合法在这里同步拒绝，不让坏数据进外部队列。
"""

import re
from dataclasses import replace

from app.config.constant import Constant
from app.models import (
    ConversationRunAttachmentInput,
    ConversationRunCommand,
    ConversationRunFileAttachment,
)
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions, ReasoningEffort
from app.models.errors.input_errors import UserInputRejectedError
from app.core.workflow.message_queue.task_message import Delivery, RunLaunch, TaskMessage, UserInputBody
from app.service.attachment.attachment_service import AttachmentService
from app.service.depends import get_model_config_service, get_task_crud
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


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
            reuse: UserInputPresentation | None = None,
    ) -> TaskMessage:
        """校验模型能力、解析附件与图片，组装成携带发起配置的用户输入消息（尚未入队、没有行 id）。

        ``delivery`` 是信封属性，原样带上。

        ``reuse`` 是编辑重跑目标的展示绑定：复用其已解析的附件路径（不从 Run 猜附件归属），请求里带
        新路径的附件照常覆盖；正文不再引用的附件丢弃。图片不复用——图片用 locator 存在展示投影里，
        编辑请求照常带 image part。

        异常:
            UserInputRejectedError: 模型配置不存在、模型不支持所选能力、附件或图片不可用、输入为空。
            ImageNormalizationError: 本次图片数量或总大小超过统一输入限制。
            KeyError: Task 不存在（接入层映射为 404）。
        """
        task = self._task.get(task_id)
        # 模型配置缺失不是「寻址不到 Task」：接入层把 KeyError 统一映射为 404 task not found。
        # 本层明确知道自己查的是什么，就地换成可读的输入拒绝，避免给出错误语义。
        try:
            config = get_model_config_service().get_config(model_config_id)
        except KeyError as exc:
            raise UserInputRejectedError(
                "MODEL_CONFIG_UNAVAILABLE",
                "当前模型配置不存在，请重新选择模型",
                detail=f"model config {model_config_id} is gone",
            ) from exc
        if reasoning_effort is not None and not config.supports_reasoning_effort:
            raise UserInputRejectedError(
                "REASONING_EFFORT_UNSUPPORTED", "当前模型不支持所选的推理强度"
            )
        if command.image_asset_ids and not config.supports_image:
            raise UserInputRejectedError(
                "IMAGE_NOT_SUPPORTED", "当前模型不支持图片输入，请更换模型或移除图片"
            )
        if reuse is not None:
            command = self._reuse_attachments(command, reuse)
        self._check_image_token_bindings(command)
        file_attachments = self._resolve_file_attachments(
            command.attachments,
            display_text=command.display_text,
        )
        input_text = self._resolve_file_tokens(command.display_text, file_attachments)
        image_paths = self._resolve_image_assets(task.workspace_id, command.image_asset_ids)
        # wire schema 已保证文本 part 非空，但内部发送方（delegate_task / child_agent_send /
        # Team 节点）直接构造命令，且「正文只剩图片 token」时替换后同样是空。
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
        # 编辑可携带原消息的全部绑定；只解析当前正文仍引用的附件。
        for attachment_id in dict.fromkeys(
                m.group(1) for m in find_message_tokens(Constant.Cosir.LOCAL_FILE_TOKEN, display_text)
        ):
            attachment = by_id.get(attachment_id)
            if attachment is None or attachment.path is None:
                reason = "no matching request attachment" if attachment is None else "missing path"
                raise UserInputRejectedError(
                    "ATTACHMENT_UNAVAILABLE",
                    "附件已失效，请重新选择附件",
                    detail=f"ordinary file attachment {attachment_id} is unavailable ({reason})",
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
    def _resolve_file_tokens(
            text: str,
            attachments: list[ConversationRunFileAttachment],
    ) -> str:
        """把正文里的附件 token 换成模型可读内容：文件换路径，图片由 image part 承载、正文丢弃。

        必须单趟扫描两类 token：分两趟时第一趟插入的文件路径会被第二趟当成原文再解析，路径里含
        token 或 Markdown 围栏就会把正文改坏。
        """
        paths = {attachment["id"]: attachment["path"] for attachment in attachments}

        def substitute(match: re.Match[str]) -> str:
            kind, token_id = match.group(1), match.group(2)
            return paths[token_id] if kind == "file" else ""

        return replace_message_tokens(Constant.Cosir.LOCAL_INPUT_TOKEN, substitute, text)

    @staticmethod
    def _resolve_image_assets(workspace_id: int, image_asset_ids: list[str]) -> list[str]:
        """校验本次图片聚合限制，并返回已上传图片的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        return AttachmentService().resolve_image_assets_for_run(workspace_id, image_asset_ids)

    @staticmethod
    def _reuse_attachments(
            command: ConversationRunCommand, previous: UserInputPresentation
    ) -> ConversationRunCommand:
        """合并编辑目标已解析的附件绑定；请求里带新路径的同 id 附件覆盖旧绑定。

        页面重载后前端丢失本地附件登记、只剩正文里的 token，路径只能从这里恢复。合并后没有被正文
        引用的附件由 ``_resolve_file_attachments`` 丢弃。
        """
        attachments = {
            a["id"]: ConversationRunAttachmentInput(
                id=a["id"], name=a["name"], content_type=a["content_type"], path=a["path"]
            )
            for a in previous.attachments
        }
        for attachment in command.attachments:
            if attachment.path is not None:
                attachments[attachment.id] = attachment
        return replace(command, attachments=list(attachments.values()))
