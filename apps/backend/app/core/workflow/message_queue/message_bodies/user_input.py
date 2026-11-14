"""用户输入消息：胶囊标签正文、展示绑定与它携带的发起配置。

模型正文与展示绑定是同一份正文：入队前校验并规范化的胶囊文本（``<file>`` / ``<ref>`` /
``<skill>`` 指针，Skill 已补 ``path``）既进模型，也供快照投影与编辑复用。图片只带路径，
发请求前才由 ``vision_input`` 解析成 data URL。
"""

from typing import TYPE_CHECKING, ClassVar

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict

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
from app.models.conversation_input import UserInputPresentation
from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.message_metadata import UserMessageMetadata

if TYPE_CHECKING:
    from app.models import RunRecord, SessionRecord


class RunLaunch(BaseModel):
    """一个 Run 的发起配置；活动 Run 期间锁定，交接新 Run 与入队校验共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 交接新 Run 时的 Agent 身份；Session 不绑定 Agent，身份随发起配置走。
    agent_id: str = "main_agent"
    model_config_id: str
    reasoning_effort: ReasoningEffort | None = None

    @classmethod
    def from_run(cls, run: "RunRecord | None") -> "RunLaunch | None":
        """沿用某个 Run 已冻结的发起配置；Run 没记录 Agent 或模型时返回 ``None``。

        模型被删后 Run 仍保留原 id，这里照样返回；能否开新 Run 由 ``SessionActor`` 查模型是否还在。
        """

        if run is None or run.agent_id is None or run.model_config_id is None:
            return None
        return cls(
            agent_id=run.agent_id,
            model_config_id=run.model_config_id,
            reasoning_effort=run.reasoning_effort,
        )

    @classmethod
    def from_fork(cls, session: "SessionRecord") -> "RunLaunch | None":
        """分支还没有本地 Run 时沿用分叉时冻结的初始配置；不依赖来源 Session 是否还在。

        冻结的模型 id 只是来历，不保证还存在：入队前的消息准备会校验所选模型，用户也可以换。
        不是分支，或冻结配置缺 Agent / 模型（已被删除）时返回 ``None``。
        """

        fork = session.extra.fork
        if fork is None:
            return None
        launch = fork.initial_launch
        if launch.agent_id is None or launch.model_config_id is None:
            return None
        return cls(
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            reasoning_effort=launch.reasoning_effort,
        )


class UserInputBody(EnqueuedMessageBody):
    """用户输入：入队前已校验并规范化胶囊指针；唯一可编辑的消息，与 Agent 消息一样可交接新 Run。"""

    KIND: ClassVar[SessionMessageKind] = "user_input"
    TAG: ClassVar[str] = "user_input"
    PROMPT_GUIDE: ClassVar[str | None] = (
        "the user's own words to you. Capsules in it are pointers the user attached: "
        "`<file path>` is a local file, `<ref path>` a workspace path (an empty path means "
        "the workspace root) with optional `lines` or `page`, `<skill name path>` a Skill "
        "the user picked. Read them with read_file (PDFs through a Skill or script); for "
        "`<skill>`, read its `path` or call skill(action='load') first, then follow it. "
        "`image_paths` lists attached images by local path."
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
