"""用户消息事实：未消费时归 inbox，消费后归 context，不在 Run 留副本。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment
from app.models.conversation_run_options import ConversationRunOptions
from app.utils.message_tokens import find_message_tokens


class UserInputPresentation(BaseModel):
    """随消息持久化的展示绑定；不重复保存 SDK 消息正文或 Run 执行配置。"""

    model_config = ConfigDict(extra="forbid")
    display_text: str
    image_paths: list[str] = Field(default_factory=list)
    attachments: list[ConversationRunFileAttachment] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_attachment_tokens(self) -> "UserInputPresentation":
        """输入边界保证展示 token 与附件身份一致；文件存在性由附件解析负责。"""
        for item in self.attachments:
            if (
                not Constant.Cosir.LOCAL_FILE_ID.fullmatch(item["id"])
                or not item["name"]
                or not item["content_type"]
                or not item["path"].strip()
            ):
                raise ValueError("attachment fields are invalid")
        ids = [item["id"] for item in self.attachments]
        tokens = list(
            dict.fromkeys(
                match.group(1)
                for match in find_message_tokens(Constant.Cosir.LOCAL_FILE_TOKEN, self.display_text)
            )
        )
        if ids != tokens or len(ids) != len(set(ids)):
            raise ValueError("display_text attachment tokens do not match attachments")
        return self


class ConversationInput(UserInputPresentation):
    """入队前解析好的用户输入；消费时正文转为 SDK 消息，展示绑定单独保存。"""

    input_text: str

    def presentation(self) -> UserInputPresentation:
        """只导出渲染与编辑所需的绑定，模型正文在 context.message 中保存一次。"""
        return UserInputPresentation(
            display_text=self.display_text,
            image_paths=self.image_paths,
            attachments=self.attachments,
        )


class QueuedTaskInput(BaseModel):
    """输入连同发起配置排队；交接新 Run 时采用配置，消费到 context 时只转移消息。"""

    model_config = ConfigDict(extra="forbid")
    message: ConversationInput
    model_config_id: int
    options: ConversationRunOptions
