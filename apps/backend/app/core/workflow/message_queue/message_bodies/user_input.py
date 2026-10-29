"""用户输入消息：进模型的正文、展示绑定与它携带的发起配置。

模型正文与展示绑定分开保存、互不派生：``input_text``（token 已换成路径）进模型，``presentation``
（编辑器原样正文、图片与附件描述符）供快照投影、编辑复用与 fork。
"""

from typing import TYPE_CHECKING, ClassVar

from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_image_param import ImageURL
from pydantic import BaseModel, ConfigDict, Field

from app.core.workflow.message_queue.message_bodies.base import EnqueuedMessageBody, TaskMessageKind
from app.core.workflow.message_queue.message_bodies.rendering import render_time
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
    """用户输入：入队前已解析好正文与附件，唯一可编辑、可交接新 Run 的消息。"""

    KIND: ClassVar[TaskMessageKind] = "user_input"
    TAG: ClassVar[str] = "user_input"
    PROMPT_GUIDE: ClassVar[str] = (
        "the user talking to you, and the only source of new requests. `image_paths` lists "
        "attached images by local path."
    )
    PRIORITY: ClassVar[int] = 1
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKES: ClassVar[bool] = True
    input_text: str
    launch: RunLaunch
    presentation: UserInputPresentation
    # 父 Agent 经 delegate_task / child_agent_send 投递时那次调用的身份；用户直接输入为 None。
    # 不属于发起配置：不参与运行期配置比较，只随新 Run 记到 Run 上（见 ``StartRun``）。
    trigger_tool_call_id: str | None = None

    @property
    def text(self) -> str:
        img_paths = "".join(f"\n- {path}" for path in self.presentation.image_paths)
        if img_paths:
            img_paths = "\nimage_paths:" + img_paths
        return (f'<{self.TAG} time="{render_time()}">'
                f"{self.input_text}{img_paths}"
                f"</{self.TAG}>")

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """模型正文（图片用本地 locator，发请求前才解析，不落盘 base64）与展示绑定分开返回。"""

        metadata = UserMessageMetadata(kind="user_input", presentation=self.presentation)
        if not self.presentation.image_paths:
            return ChatCompletionUserMessageParam(role="user", content=self.text), metadata
        content: list[ChatCompletionContentPartParam] = []
        if self.input_text:
            content.append(ChatCompletionContentPartTextParam(text=self.text, type="text"))
        content.extend(
            ChatCompletionContentPartImageParam(
                image_url=ImageURL(url=f"cosir-attachment://{path}"),
                type="image_url"
            )
            for path in self.presentation.image_paths
        )
        return ChatCompletionUserMessageParam(role="user", content=content), metadata
