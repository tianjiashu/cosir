"""工具产出的图片：tool 消息只能是文本，图片随后以一条 user 消息交给模型。"""

from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict

from app.core.workflow.message_queue.message_bodies.rendering import (
    render_time,
    user_message_with_images,
)
from app.models.message_metadata import UserMessageMetadata


class ToolImageBody(BaseModel):
    """一次工具调用产出的图片（如浏览器截图）。

    图片是这次调用结果的一部分，不是一条消息：observe 节点在整批 tool 消息之后直接写进 context
    （``write_tool_images``），与 tool 消息落在同一步。因此不继承 :class:`EnqueuedMessageBody`、不入
    队，也就不会像曾经走 mailbox 那样，在挂起、取消或步数用完时随执行被丢弃。不投影：卡片自己展示
    截图。
    """

    model_config = ConfigDict(extra="forbid")
    TAG: ClassVar[str] = "tool_image"
    PROMPT_GUIDE: ClassVar[str] = (
        "images produced by the tool call with that tool_call_id (for example a browser "
        "screenshot); they are part of that call's result."
    )
    tool_call_id: str
    tool_name: str
    image_paths: list[str]

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        label = (
            f'<{self.TAG} time="{render_time()}" tool_call_id="{self.tool_call_id}" '
            f'tool_name="{self.tool_name}" />'
        )
        return (
            user_message_with_images(label, self.image_paths),
            UserMessageMetadata(kind="tool_image", tool_call_id=self.tool_call_id),
        )
