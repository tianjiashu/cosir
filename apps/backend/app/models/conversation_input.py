"""用户消息事实：未消费时归 inbox，消费后归 context，不在 Run 留副本。"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant
from app.models.conversation_run_file_attachment import ConversationRunFileAttachment
from app.utils.message_tokens import find_message_tokens


def image_asset_id(path: str) -> str:
    """从已发布图片的 workspace 相对路径推导资源 id（文件名去掉扩展名）。

    附件存储固定为 ``<asset_id>.<ext>``。展示绑定校验与展示投影必须用同一规则推导 id，
    否则同一份 presentation 在「能不能渲染」上会得到两种结论。
    """
    return Path(path).name.split(".", 1)[0]


class UserInputPresentation(BaseModel):
    """随消息持久化的展示绑定；不重复保存 SDK 消息正文或 Run 执行配置。"""

    model_config = ConfigDict(extra="forbid")
    display_text: str
    image_paths: list[str] = Field(default_factory=list)
    attachments: list[ConversationRunFileAttachment] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_attachment_tokens(self) -> "UserInputPresentation":
        """展示绑定必须自洽：正文引用的每个 token 都能解析成对应附件/图片。

        这是持久化事实的形状约束（队列 body、context metadata、展示投影都按它消费），不校验
        文件是否存在——附件路径只是给模型和工具的引用，存在性由真正读取它的地方判定。
        图片方向尤其重要：图片正文不进模型文本，漏绑不会在正文里露馅，只会在投影时炸掉。
        """
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
        image_ids = {image_asset_id(path) for path in self.image_paths}
        if any(
            match.group(1) not in image_ids
            for match in find_message_tokens(Constant.Cosir.LOCAL_IMAGE_TOKEN, self.display_text)
        ):
            raise ValueError("display_text image tokens do not match images")
        return self

