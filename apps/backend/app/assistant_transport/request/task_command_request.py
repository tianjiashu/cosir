"""Task 级命令的 wire schema：发送消息、编辑排队消息、编辑重跑、Agent Team 审批决定。

命令只对 Task（路径中的 ``task_id``）或消息（路径中的 ``message_id``）寻址，不携带 run_id：Run
身份由后端按 Task 的最新 Run 解析。wire 字段沿用 camelCase，进入 service 层前映射为领域对象。
"""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.assistant_transport.request.assistant_user_message import AssistantUserMessage
from app.assistant_transport.request.part import AssistantImagePart, AssistantTextPart
from app.config.constant import Constant
from app.models import ConversationRunAttachmentInput, ConversationRunCommand
from app.models.conversation_run_options import ReasoningEffort
from app.core.workflow.inbox.task_message import Delivery
from app.utils.message_tokens import find_message_tokens, replace_message_tokens


def _ordered_display_text(parts: Sequence[AssistantTextPart | AssistantImagePart]) -> str:
    """将 composer 的文本/图片顺序编码进输入消息的展示文本。"""

    image_token = Constant.Cosir.LOCAL_IMAGE_TOKEN
    emitted_image_ids = {
        match.group(1)
        for part in parts
        if isinstance(part, AssistantTextPart)
        for match in find_message_tokens(image_token, part.text)
    }
    segments: list[str] = []
    for part in parts:
        if isinstance(part, AssistantTextPart):
            segments.append(
                replace_message_tokens(Constant.Transport.HIDDEN_LOCAL_FILE_TOKEN, r"\1", part.text)
            )
        else:
            asset_id = part.image.removeprefix("cosir-attachment://")
            if asset_id in emitted_image_ids:
                continue
            segments.append(f"[[cosir-image:{asset_id}]]")
            emitted_image_ids.add(asset_id)
    return "\n".join(segments)


class _MessageCommandRequest(BaseModel):
    """消息命令：把 wire 消息映射成 Run 命令；发起配置由需要开 Run 的子类补充。"""

    model_config = ConfigDict(extra="forbid")

    message: AssistantUserMessage

    def to_run_command(self) -> ConversationRunCommand:
        """``display_text`` 保持文字与图片的原始顺序；附件绑定随输入进入 inbox。"""

        return self._run_command(ban_tools=[])

    def _run_command(self, *, ban_tools: list[str]) -> ConversationRunCommand:
        parts = self.message.parts
        return ConversationRunCommand(
            display_text=_ordered_display_text(parts),
            image_asset_ids=list(
                dict.fromkeys(
                    part.image.removeprefix("cosir-attachment://")
                    for part in parts
                    if isinstance(part, AssistantImagePart)
                )
            ),
            ban_tools=ban_tools,
            attachments=[
                ConversationRunAttachmentInput(
                    id=attachment.id,
                    name=attachment.name,
                    content_type=attachment.contentType,
                    path=attachment.path,
                )
                for attachment in self.message.attachments
            ],
        )


class _LaunchCommandRequest(_MessageCommandRequest):
    """会开出或并入 Run 的消息命令：附带发起配置。"""

    # 模型名由后端配置事实读取，请求只携带配置身份。
    modelConfigId: int = Field(ge=1)
    reasoningEffort: ReasoningEffort | None = None
    banTools: list[StrictStr] = Field(default_factory=list)

    @field_validator("banTools")
    @classmethod
    def _validate_ban_tools(cls, value: list[str]) -> list[str]:
        if any(not name.strip() for name in value):
            raise ValueError("banTools entries must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("banTools entries must be unique")
        return value

    def to_run_command(self) -> ConversationRunCommand:
        return self._run_command(ban_tools=list(self.banTools))


class SendMessageRequest(_LaunchCommandRequest):
    """发送消息；``delivery`` 只在 Task 有活动 Run 时生效（立即并入 / 交给下一个 Run）。"""

    delivery: Delivery = "immediate"


class EditQueuedMessageRequest(_MessageCommandRequest):
    """原地编辑一条排队中（未并入）的用户消息：只带新正文与附件。

    发起配置（模型、推理强度、禁用工具）在发送那一刻已定，编辑沿用，因此请求里没有这些字段。
    """


class RerunMessageRequest(_LaunchCommandRequest):
    """编辑最新一条已并入的用户消息并重跑：按本次请求的配置开新 Run。"""


class TeamDecisionRequest(BaseModel):
    """对一个待确认 Agent Team 方案作出决定；``data`` 的形状由决定种类定义（批准：最终运行输入，
    驳回：``feedback``），在 ``AgentTeamDecisionService`` 入队边界校验。"""

    model_config = ConfigDict(extra="forbid")

    teamRunId: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    data: dict[str, Any] = Field(default_factory=dict)
