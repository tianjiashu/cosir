"""用户输入消息：胶囊标签正文、展示绑定与它携带的发起配置。

模型正文与展示绑定是同一份正文：入队前校验并规范化的胶囊文本（``<file>`` / ``<ref>`` /
``<skill>`` 指针，Skill 已补 ``path``）既进模型，也供快照投影与编辑复用。图片只带路径，
发请求前才由 ``vision_input`` 解析成 data URL。
"""

from typing import ClassVar

from openai.types.chat import ChatCompletionUserMessageParam

from app.core.workflow.message_queue.message_bodies.base import (
    EnqueuedMessageBody,
    SessionMessageKind,
    WakeScope,
)
from app.core.workflow.message_queue.message_bodies.rendering import (
    escape_tag,
    render_time,
    user_message_with_images,
)
from app.core.workflow.message_queue.message_bodies.run_launch import RunLaunch
from app.models.message_context.message_metadata.user_message_metadata import (
    UserInputPresentation,
    UserMessageMetadata,
)


class UserInputBody(EnqueuedMessageBody):
    """用户输入：入队前已校验并规范化胶囊指针；唯一可编辑的消息，与 Agent 消息一样可交接新 Run。"""

    KIND: ClassVar[SessionMessageKind] = "user_input"
    TAG: ClassVar[str] = "user_input"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "the user's own words to you. Capsules in it are pointers the user attached: "
        "`<file path>` is a local file, `<ref path>` a workspace path (an empty path means "
        "the workspace root) with optional `lines` or `page`, `<skill name path>` a Skill "
        "the user picked for this request (load it, then follow it). `image_paths` lists the "
        "local paths of the images attached to this message; the images themselves are "
        "included."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKE: ClassVar[WakeScope] = "new_or_active"
    text: str
    launch: RunLaunch
    presentation: UserInputPresentation

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """模型正文与展示绑定同一份；有图时图片以本地 locator 随消息发出，不落盘 base64。"""

        metadata = UserMessageMetadata(kind="user_input", presentation=self.presentation)
        # 有图无字也照常发标签：输入协议承诺每条 user 消息都带一个标签，空标签比无标签好认。
        content = user_message_with_images(self._tagged_text(), self.presentation.image_paths)
        return content, metadata

    def _tagged_text(self) -> str:
        """起始标签的 ``hint`` 提醒模型遵循系统提示词的 ``output_layer``。

        标签正文是用户输入的胶囊正文，转义同名标签后放入，用户输入里的 ``</user_input>``
        不能伪造消息边界；有图片时在原文之后逐行列出路径。
        """

        img_paths = "".join(f"\n- {path}" for path in self.presentation.image_paths)
        if img_paths:
            img_paths = "\nimage_paths:" + img_paths
        return (
            f'<{self.TAG} time="{render_time()}" hint="Follow the output_layer rules.">'
            f"{escape_tag(self.text, self.TAG)}{img_paths}"
            f"</{self.TAG}>"
        )
