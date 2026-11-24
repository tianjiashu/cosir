"""用户输入消息：胶囊标签正文、展示绑定与它携带的发起配置。

展示绑定是入队前校验并规范化的胶囊文本（``<file>`` / ``<ref>`` / ``<skill>`` 指针，Skill 已补
``path``；``<quote>`` 摘录），供快照投影与编辑复用。模型正文由它派生：指针原样保留，摘录展开成
``<quote>`` 块——属性里转义成单行的原文对模型是噪声，来源定位只服务界面。图片只带路径，发请求前才由
``vision_input`` 解析成 data URL。
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
from app.models.message_context.capsules.capsule_grammar import CapsuleSegment, Segment
from app.models.message_context.capsules.grammar.quote_capsule import QuoteCapsule
from app.models.message_context.message_metadata.user_message_metadata import (
    UserInputPresentation,
    UserMessageMetadata,
)


class UserInputBody(EnqueuedMessageBody):
    """用户输入：入队前已校验并规范化胶囊指针；唯一可编辑的消息，与 Agent 消息一样可交接新 Run。"""

    KIND: ClassVar[SessionMessageKind] = "user_input"
    TAG: ClassVar[str] = "user_input"
    QUOTE_TAG: ClassVar[str] = "quote"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "the user's own words to you. Capsules in it are pointers the user attached: "
        "`<file path>` is a local file, `<ref path>` a workspace path (an empty path means "
        "the workspace root) with optional `lines` or `page`, `<skill name path>` a Skill "
        "the user picked for this request (load it, then follow it). A `<quote>` block is a "
        "verbatim excerpt the user selected, from an earlier assistant reply (in this conversation "
        "or another session such as a sub-agent's) or, when it has a `source`, from that "
        "workspace file location as it read then (read the file for context); the line right after "
        "it is the user's remark on that excerpt, and the user's words are the request, not the "
        "excerpt. "
        "`image_paths` lists the "
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
            f"{escape_tag(self._model_text(), self.TAG)}{img_paths}"
            f"</{self.TAG}>"
        )

    def _model_text(self) -> str:
        """模型看到的正文：摘录胶囊展开成独占几行的 ``<quote>`` 块，批注紧跟其后；原文转义同名标签防伪造边界。"""

        return "".join(
            self._quote_block(segment.capsule)
            if isinstance(segment, CapsuleSegment) and isinstance(segment.capsule, QuoteCapsule)
            else segment.serialize()
            for segment in Segment.parse(self.text)
        )

    @classmethod
    def _quote_block(cls, quote: QuoteCapsule) -> str:
        source = quote.source()
        attribute = f' source="{source.replace(chr(34), "&quot;")}"' if source else ""
        note = f"{quote.note}\n" if quote.note else ""
        return f"\n<{cls.QUOTE_TAG}{attribute}>\n{escape_tag(quote.text, cls.QUOTE_TAG)}\n</{cls.QUOTE_TAG}>\n{note}"
