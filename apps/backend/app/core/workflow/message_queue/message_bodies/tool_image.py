"""工具产出的图片：tool 消息只能是文本，图片随后以一条 user 消息交给模型。"""

from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import (
    render_time,
    user_message_with_images,
)
from app.models.message_metadata import UserMessageMetadata


class ToolImageBody(EnqueuedMessageBody):
    """一次工具调用产出的图片（如浏览器截图）。

    只由 observe 节点在整批 tool 消息写完后投进本次执行的 mailbox（``post_tool_images``），不落外部
    队列；请求模型前由 prepare_input 取出写入 context。与 Run 内通知同一
    取舍：工具结束到下一次请求之间 Run 被取消或挂起时，mailbox 随执行丢弃，这张图不进 context——
    tool 消息文本与附件文件都还在，模型需要可以重新取图。

    排在通知与用户输入之前：图片属于刚结束的那一批工具调用。不投影：卡片自己展示截图。
    """

    KIND: ClassVar[TaskMessageKind] = "tool_image"
    TAG: ClassVar[str] = "tool_image"
    PROMPT_GUIDE: ClassVar[str] = (
        "images produced by the tool call with that tool_call_id (for example a browser "
        "screenshot); they are part of that call's result."
    )
    PRIORITY: ClassVar[int] = -1
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES: ClassVar[bool] = False
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
