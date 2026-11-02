"""用户输入消息：进模型的正文、展示绑定与它携带的发起配置。

模型正文与展示绑定分开保存、互不派生：``input_text``（token 已换成路径）进模型，``presentation``
（编辑器原样正文、图片与附件描述符）供快照投影、编辑复用与 fork。
"""

from typing import TYPE_CHECKING, ClassVar

from openai.types.chat import ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict, Field

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import (
    render_time,
    user_message_with_images,
)
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions
from app.models.message_metadata import UserMessageMetadata

if TYPE_CHECKING:
    from app.models import ConversationRunRecord


class RunLaunch(BaseModel):
    """一个 Run 的发起配置；活动 Run 期间锁定，交接新 Run 与入队校验共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 交接新 Run 时的 Agent 身份；Task 不绑定 Agent，身份随发起配置走。
    agent_id: str = "main_agent"
    model_config_id: int
    options: ConversationRunOptions = Field(default_factory=ConversationRunOptions)

    @classmethod
    def from_run(cls, run: "ConversationRunRecord | None") -> "RunLaunch | None":
        """沿用某个 Run 已冻结的发起配置；Run 缺 Agent 或模型配置（已被删除）时返回 ``None``。"""

        if run is None or run.agent_id is None or run.model_config_id is None:
            return None
        return cls(
            agent_id=run.agent_id,
            model_config_id=run.model_config_id,
            options=run.options,
        )


class UserInputBody(EnqueuedMessageBody):
    """用户输入：入队前已解析好正文与附件；唯一可编辑的消息，与 Agent 消息一样可交接新 Run。"""

    KIND: ClassVar[TaskMessageKind] = "user_input"
    TAG: ClassVar[str] = "user_input"
    PROMPT_GUIDE: ClassVar[str] = (
        "the user talking to you. `image_paths` lists attached images by local path."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKES: ClassVar[bool] = True
    input_text: str
    launch: RunLaunch
    presentation: UserInputPresentation

    @property
    def text(self) -> str:
        """标签正文：用户输入、图片清单，末端附一句「按 ``<output_layer>`` 输出」的提醒。

        提醒每轮都写进 canonical 历史（约 6 token）：长对话里模型会按自己的习惯漂移格式（尤其
        回复语言与文件链接），靠系统提示词之外的最近一句把它拉回来。代价是历史随轮次线性增长，
        且这句位于 ``<user_input>`` 内、会被读成用户引言——规则本体在 ``<output_layer>``，
        这里只指路，不重复内容。
        """

        img_paths = "".join(f"\n- {path}" for path in self.presentation.image_paths)
        if img_paths:
            img_paths = "\nimage_paths:" + img_paths
        return (
            f'<{self.TAG} time="{render_time()}" hint:"Output please follow <output_layer> rules ">'
            f"{self.input_text}{img_paths}"
            f"</{self.TAG}>"
        )

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """模型正文（图片用本地 locator，发请求前才解析，不落盘 base64）与展示绑定分开返回。"""

        metadata = UserMessageMetadata(kind="user_input", presentation=self.presentation)
        # 有图无字也照常发标签：输入协议承诺每条 user 消息都带一个标签，空标签比无标签好认。
        return user_message_with_images(self.text, self.presentation.image_paths), metadata
