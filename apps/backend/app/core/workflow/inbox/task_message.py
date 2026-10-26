"""Task 消息队列的消息实体：外部持久化队列与 Run 内投递通道共用同一组类型。

所有消息进模型时都是 user 角色，来源由 ``TaskMessage.kind`` 表达（由 body 类型决定，持久化时按
它还原 body），后端规则只读 ``kind``、不解析正文。何时并入只看 ``delivery``，是否投影只看
``project_to_transport``，投递方不做额外判断；两者的默认值由 body 类型决定，生产者可覆盖。能否
唤醒也是类型规则（``WAKES_SUSPENDED`` / ``WAKES_COMPLETED``），由 ``dispatch_policy`` 读取。实体只负责「如何变成 context 消息」，投递时机归
``TaskActors``，写入归 ``prepare_input``。

非用户输入的正文由后端加标签包装；外部来源文本（用户反馈、子 Agent / 后台任务输出）里出现的同名
标签先转义，防止伪造边界。
"""
import json
from collections.abc import Sequence
from datetime import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionUserMessageParam, \
    ChatCompletionContentPartTextParam, ChatCompletionContentPartImageParam
from openai.types.chat.chat_completion_content_part_image_param import ImageURL
from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation_input import ConversationInput, UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions
from app.models.enums.react_route import ReactRoute
from app.models.message_metadata import UserMessageMetadata
from app.utils.host_platform import host_platform_name

if TYPE_CHECKING:
    from app.storage.model.task_message_model import TaskMessageModel

Delivery = Literal["immediate", "next_run"]
"""并入时机：``immediate`` 在当前执行的下一次模型请求前并入；``next_run`` 只交给下一个 Run。"""

TaskMessageKind = Literal["user_input", "system_notice", "background_result", "user_decision"]


class RunLaunch(BaseModel):
    """一个 Run 的发起配置；活动 Run 期间锁定，交接新 Run 与入队校验共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 交接新 Run 时的 Agent 身份；Task 不绑定 Agent，身份随发起配置走。
    agent_id: str = "main_agent"
    model_config_id: int
    options: ConversationRunOptions = Field(default_factory=ConversationRunOptions)


class UserInputBody(UserInputPresentation):
    """用户输入：入队前已解析好正文与附件，携带发起配置；唯一可编辑、可交接新 Run 的消息。"""

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind] = "user_input"
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKES_SUSPENDED: ClassVar[bool] = True
    # 已完成 Task 收到用户输入时按其自身配置交接新 Run（见 dispatch_policy），不靠继承唤醒。
    WAKES_COMPLETED: ClassVar[bool] = False
    input_text: str
    launch: RunLaunch

    @property
    def text(self) -> str:
        img_paths = ''
        if self.image_paths:
            img_paths = "\nimage_paths:"
            img_paths += "\n".join(self.image_paths)
        return (f'<user_input time="{_render_time()}">'
                f"{self.input_text}{img_paths}"
                f"</user_input>")

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

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 统一加；
    ``notice_kind`` / ``metadata`` 只服务观测，不进模型正文。
    """

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind] = "system_notice"
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = False
    WAKES_COMPLETED: ClassVar[bool] = False
    content: str
    notice_kind: str
    metadata: dict[str, str | None] = Field(default_factory=dict)

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        return notices_to_context([self])


class DecisionBody(BaseModel):
    """用户对一次等待决定的工具调用（HITL）作出的决定。

    批准时 ``prepare_input`` 绕过模型、直接执行 ``tool_call_id`` 对应的原调用（参数从 context 里的
    原调用取，不随消息保存）；驳回时作为一条决定消息交给模型。一轮只处理一条决定，且先于其他消息
    处理。用户主动作出的决定可以唤醒挂起的 Run，也能在 Run 已完成、取消或失败后开出新 Run。
    """

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind] = "user_decision"
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = True
    WAKES_COMPLETED: ClassVar[bool] = True
    tool_call_id: str
    tool_name: str
    decision: Literal["approve", "reject"]
    feedback: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        fb = ""
        if self.feedback:
            fb = f"\nfeedback: {escape_tag(self.feedback, 'user_decision')}"
        content = (
            f'<user_decision time="{_render_time()}" tool_call_id="{self.tool_call_id}" '
            f'tool_name="{self.tool_name}">'
            f"\ndecision: {self.decision}{fb}\n</user_decision>"
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(kind="decision"),
        )


class BackgroundResultBody(BaseModel):
    """委派出去的子 Agent 一次 Run 的结论；唤醒挂起的父 Agent，父 Run 已完成时也唤醒。

    子 Task 每次 Run 到终态投递一条（多次追加消息就有多条，逐条到达）；因父 Agent 停止而被取消的
    子 Run 不投递。不投影：委派卡片的状态已跟随子 Task 最新 Run。
    """

    model_config = ConfigDict(extra="forbid")
    KIND: ClassVar[TaskMessageKind] = "background_result"
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = True
    WAKES_COMPLETED: ClassVar[bool] = True
    tool_name: str
    tool_result: str

    def render(self) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
        content = (
            f'<background_result time="{_render_time()}" tool_name="{self.tool_name}">\n'
            f"{escape_tag(self.tool_result, 'background_result')}\n</background_result>"
        )
        return (
            ChatCompletionUserMessageParam(role="user", content=content),
            UserMessageMetadata(kind="background_result"),
        )


MessageBody = UserInputBody | NoticeBody | DecisionBody | BackgroundResultBody

_BODY_OF_KIND: dict[str, type[MessageBody]] = {
    body.KIND: body for body in (UserInputBody, NoticeBody, DecisionBody, BackgroundResultBody)
}


@dataclass(frozen=True)
class TaskMessage:
    """一条队列消息；``id`` 是外部队列行 id，也是排队与已消费阶段共用的消息身份。

    Run 内产生的提示只投进本次执行的 ``RunMailbox``，``id`` 为 ``None``、无需确认，随执行丢弃。
    ``kind`` 由 body 类型决定（经 :meth:`of` 构造），持久化时单独成列，读回时按它还原 body。
    """

    id: int | None
    kind: TaskMessageKind
    body: MessageBody
    delivery: Delivery = "immediate"
    project_to_transport: bool = True

    @classmethod
    def of(
        cls,
        body: MessageBody,
        *,
        delivery: Delivery = "immediate",
        project_to_transport: bool | None = None,
        message_id: int | None = None,
    ) -> "TaskMessage":
        """按 body 类型补齐 ``kind`` 与投影默认值。"""
        if project_to_transport is None:
            project_to_transport = type(body).DEFAULT_PROJECTED
        return cls(
            id=message_id,
            kind=type(body).KIND,
            body=body,
            delivery=delivery,
            project_to_transport=project_to_transport,
        )

    @classmethod
    def of_model(cls, row: "TaskMessageModel") -> "TaskMessage":
        """持久化边界：按 ``kind`` 列校验并还原 body；未知 kind 或形状不符直接失败。"""
        body = _BODY_OF_KIND[row.kind].model_validate(row.payload)
        return cls(
            id=row.id,
            kind=row.kind,
            body=body,
            delivery=row.delivery,
            project_to_transport=row.project_to_transport,
        )


def escape_tag(text: str, tag: str) -> str:
    """转义外部文本中的同名标签，防止伪造消息边界；其余内容原样保留。"""
    return text.replace(f"</{tag}", f"&lt;/{tag}").replace(f"<{tag}", f"&lt;{tag}")


def _render_time() -> str:
    """渲染时刻的本地 ISO 8601（含时区偏移）。

    秒级精度足够表达消息先后，毫秒只会给模型增加噪声。取渲染时刻而不是入队时刻：body 不保存发送
    时间，同一消息在多个 Run 中渲染也只会写进 context 一次。
    """

    return datetime.now().astimezone().isoformat(timespec="seconds")


def notices_to_context(
        notices: Sequence[NoticeBody],
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """把一批通知合成一条带 ``<system_notice>`` 标签的 user 消息。

    用 user 而非 system 角色：对话中途的 system 消息在部分供应商 / 兼容层不被接受。合并而不是
    逐条写入：每条都带声明会重复占用上下文；落库后成为历史固定行，后续请求前缀不分叉。
    """
    lines = "".join(f"\n{item.content}" for item in notices)
    content = (
        f'<system_notice time="{_render_time()}">\n'
        f"Host OS is {host_platform_name()}. The lines below are runtime notices injected by the "
        "system, not user input. Take them into account, but do not reply to, acknowledge, or "
        f"quote them.{lines}\n</system_notice>"
    )
    return (
        ChatCompletionUserMessageParam(role="user", content=content),
        UserMessageMetadata(kind="notice"),
    )
