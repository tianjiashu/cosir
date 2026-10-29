"""用户消息的展示绑定：未消费时归 message_queue，消费后归 context metadata，不在 Run 留副本。"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation_run_file_attachment import ConversationRunFileAttachment


def image_asset_id(path: str) -> str:
    """从已发布图片的 workspace 相对路径推导资源 id（文件名去掉扩展名）。

    附件存储固定为 ``<asset_id>.<ext>``。展示投影必须用同一规则推导 id，否则同一份展示绑定在
    「能不能渲染」上会得到两种结论。
    """
    return Path(path).name.split(".", 1)[0]


class UserInputPresentation(BaseModel):
    """随 user 消息持久化的展示绑定；不重复保存 SDK 消息正文或 Run 执行配置。

    「正文 token ↔ 附件 / 图片」的自洽由入队边界 ``UserInputService`` 保证：那里逐项校验并按
    token 顺序绑定附件。本模型只承载已成立的事实，不再重复校验自身——它在每次快照重建与
    context 读取时都会被 ``model_validate``，为上游已保证的数据反复付出代价没有收益。
    """

    model_config = ConfigDict(extra="forbid")
    display_text: str
    image_paths: list[str] = Field(default_factory=list)
    attachments: list[ConversationRunFileAttachment] = Field(default_factory=list, max_length=32)
