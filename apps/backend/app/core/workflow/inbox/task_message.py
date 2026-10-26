"""Task 消息队列的消息实体：外部持久化队列与 Run 内投递通道共用同一组类型。

所有消息进模型时都是 user 角色，来源由 ``body.kind`` 表达，后端规则只读 ``kind``、不解析正文。
何时并入只看 ``delivery``，是否投影只看 ``project_to_transport``，投递方不做额外判断；两者的
默认值由 body 类型决定，生产者可覆盖。能否唤醒也是类型规则（``WAKES_SUSPENDED`` /
``WAKES_COMPLETED``），由 ``dispatch_policy`` 读取。实体只负责「如何变成 context 消息」，投递时机归
``TaskActors``，写入归 ``prepare_input``。

非用户输入的正文由后端加标签包装；外部来源文本（用户反馈、子 Agent / 后台任务输出）里出现的同名
标签先转义，防止伪造边界。
"""
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, cast

from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionUserMessageParam, \
    ChatCompletionContentPartTextParam, ChatCompletionContentPartImageParam
from openai.types.chat.chat_completion_content_part_image_param import ImageURL
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models.conversation_input import ConversationInput, UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions
from app.models.enums.react_route import ReactRoute
from app.models.message_metadata import UserMessageMetadata
from app.utils.host_platform import host_platform_name

if TYPE_CHECKING:
    from app.storage.model.task_message_model import TaskMessageModel

Delivery = Literal["immediate", "next_run"]
"""并入时机：``immediate`` 在当前执行的下一次模型请求前并入；``next_run`` 只交给下一个 Run。"""


class UserInputBody(UserInputPresentation):
    """用户输入：入队前已解析好正文与附件，携带发起配置；唯一可编辑、可交接新 Run 的消息。"""

    model_config = ConfigDict(extra="forbid")
    input_text: str

    @property
    def text(self) -> str:
        img_paths = ''
        if self.image_paths:
            img_paths = "\nimage_paths:"
            img_paths += "\n".join(self.image_paths)
        return (f"<user_input>"
                f"{self.input_text}{img_paths}"
                f"</use_input>")

    def presentation(self) -> UserInputPresentation:
        """只导出渲染与编辑所需的绑定，模型正文在 context.message 中保存一次。"""
        return UserInputPresentation(
            display_text=self.display_text,
            image_paths=self.image_paths,
            attachments=self.attachments,
        )

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        """模型正文（图片用本地 locator，发请求前才解析，不落盘 base64）与展示绑定分开返回。"""

        metadata = UserMessageMetadata(kind="user_input", presentation=self.presentation())
        if not self.image_paths:
            return ChatCompletionUserMessageParam(role="user", content=self.text), metadata
        content: list[ChatCompletionContentPartParam] = []
        if self.input_text:
            content.append(ChatCompletionContentPartTextParam(text=self.text, type="text"))
        content.extend(
            ChatCompletionContentPartImageParam(
                image_url=ImageURL(url=f"cosir-attachment://{path}"),
                type="image_url"
            )
            for path in self.image_paths
        )
        return ChatCompletionUserMessageParam(role="user", content=content), metadata


class NoticeBody(BaseModel):
    """系统通知：配置广播（持久化行）或 Run 内提示（只进 mailbox）；不启动 Run、不投影。

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 合并时统一加；
    ``notice_kind`` / ``metadata`` 只服务观测，不进模型正文。
    """

    model_config = ConfigDict(extra="forbid")
    content: str
    metadata: dict[str, str | None] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    """用户对 Agent Team 审批的决定；决定本身已由 service 确定性执行，这条消息只负责唤醒 Agent。"""

    model_config = ConfigDict(extra="forbid")
    tool_call_id: str
    route_tool_node: bool = False
    decision: Literal["approve", "reject"]
    feedback: str | None = None

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata | None]:
        fb = ""
        if self.feedback:
            fb = f"\nFeedback: {escape_tag(self.feedback, 'user_decision')}"

        return ChatCompletionUserMessageParam(role="user",
                                              content=f'<user_decision">decision:{self.decision}\n{fb}\n</user_decision>'), None


class BackgroundResultBody(BaseModel):
    """委派出去的子 Agent 一次 Run 的结论；唤醒挂起的父 Agent，父 Run 已完成时也唤醒。

    子 Task 每次 Run 到终态投递一条（多次追加消息就有多条，逐条到达）；因父 Agent 停止而被取消的
    子 Run 不投递。不投影：委派卡片的状态已跟随子 Task 最新 Run。
    """

    model_config = ConfigDict(extra="forbid")
    tool_name: str
    tool_result: str

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata | None]:
        content = (
            f'<background_result tool_name:{self.tool_name}>'
            f"result:{self.tool_result}</background_result>"
        )
        return ChatCompletionUserMessageParam(role="user", content=content), None


MessageBody = Annotated[
    UserInputBody | NoticeBody | DecisionBody | BackgroundResultBody,
    Field(discriminator="kind"),
]

_BODY: TypeAdapter[MessageBody] = TypeAdapter(MessageBody)


@dataclass(frozen=True)
class TaskMessage:
    """一条队列消息；``id`` 是外部队列行 id，也是排队与已消费阶段共用的消息身份。

    Run 内产生的提示只投进本次执行的 ``RunMailbox``，``id`` 为 ``None``、无需确认，随执行丢弃。
    """

    id: int | None
    kind: Literal["user_input", "system_notice", "background_result", "user_decision"]
    body: MessageBody
    delivery: Literal["immediate", "next_run"] = "immediate"
    project_to_transport: bool = True


def escape_tag(text: str, tag: str) -> str:
    """转义外部文本中的同名标签，防止伪造消息边界；其余内容原样保留。"""
    return text.replace(f"</{tag}", f"&lt;/{tag}").replace(f"<{tag}", f"&lt;{tag}")


def notices_to_context(
        notices: Sequence[NoticeBody],
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """把一批通知合成一条带 ``<system_notice>`` 标签的 user 消息。

    用 user 而非 system 角色：对话中途的 system 消息在部分供应商 / 兼容层不被接受。合并而不是
    逐条写入：每条都带声明会重复占用上下文；落库后成为历史固定行，后续请求前缀不分叉。
    """
    lines = "".join(f"\n{item.content}" for item in notices)
    content = (
        "<system_notice>\n"
        f"Host OS is {host_platform_name()}. The lines below are runtime notices injected by the "
        "system, not user input. Take them into account, but do not reply to, acknowledge, or "
        f"quote them.{lines}\n</system_notice>"
    )
    return (
        ChatCompletionUserMessageParam(role="user", content=content),
        UserMessageMetadata(kind="notice"),
    )
