"""Task 消息队列的消息实体：外部持久化队列与 Run 内投递通道共用同一组类型。

所有消息进模型时都是 user 角色，来源由 ``body.kind`` 表达，后端规则只读 ``kind``、不解析正文。
何时并入只看 ``delivery``，是否投影只看 ``project_to_transport``，投递方不做额外判断；两者的
默认值由 body 类型决定，生产者可覆盖。能否唤醒也是类型规则（``WAKES_SUSPENDED`` /
``WAKES_COMPLETED``），由 ``dispatch_policy`` 读取。实体只负责「如何变成 context 消息」，投递时机归
``WorkflowSession``，写入归 ``prepare_input``。

非用户输入的正文由后端加标签包装；外部来源文本（用户反馈、子 Agent / 后台任务输出）里出现的同名
标签先转义，防止伪造边界。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal

from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionUserMessageParam
from pydantic import BaseModel, ConfigDict, Field

from app.models.conversation_input import ConversationInput
from app.models.conversation_run_options import ConversationRunOptions
from app.models.message_metadata import UserMessageMetadata
from app.utils.host_platform import host_platform_name

Delivery = Literal["immediate", "next_run"]
"""并入时机：``immediate`` 在当前执行的下一次模型请求前并入；``next_run`` 只交给下一个 Run。"""


class RunLaunch(BaseModel):
    """一个 Run 的发起配置；活动 Run 期间锁定，交接新 Run 与入队校验共用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    # 交接新 Run 时的 Agent 身份；Task 不绑定 Agent，身份随发起配置走。
    agent_id: str = "main_agent"
    model_config_id: int
    options: ConversationRunOptions = Field(default_factory=ConversationRunOptions)


class UserInputBody(BaseModel):
    """用户输入：入队前已解析好正文与附件，携带发起配置；唯一可编辑、可交接新 Run 的消息。"""

    model_config = ConfigDict(extra="forbid")
    DEFAULT_PROJECTED: ClassVar[bool] = True
    WAKES_SUSPENDED: ClassVar[bool] = True
    # 已完成 Task 收到用户输入时按其自身配置交接新 Run（见 dispatch_policy），不靠继承唤醒。
    WAKES_COMPLETED: ClassVar[bool] = False
    kind: Literal["user_input"] = "user_input"
    message: ConversationInput
    launch: RunLaunch


class NoticeBody(BaseModel):
    """系统通知：配置广播（持久化行）或 Run 内提示（只进 mailbox）；不启动 Run、不投影。

    ``content`` 保持原始文本，``<system_notice>`` 包装由 :func:`notices_to_context` 合并时统一加；
    ``notice_kind`` / ``metadata`` 只服务观测，不进模型正文。
    """

    model_config = ConfigDict(extra="forbid")
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = False
    WAKES_COMPLETED: ClassVar[bool] = False
    kind: Literal["notice"] = "notice"
    content: str
    notice_kind: str
    metadata: dict[str, str | None] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    """用户对 Agent Team 审批的决定；决定本身已由 service 确定性执行，这条消息只负责唤醒 Agent。"""

    model_config = ConfigDict(extra="forbid")
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = True
    WAKES_COMPLETED: ClassVar[bool] = False
    kind: Literal["decision"] = "decision"
    team_run_id: int
    decision: Literal["approve", "reject"]
    feedback: str | None = None

    def render(self) -> str:
        if self.decision == "approve":
            text = (
                "The user approved the Agent Team plan. The Team is now running in the "
                "background; its result will arrive as a <background_result> message. Do not "
                "start the Team again."
            )
        else:
            text = (
                "The user rejected the Agent Team plan; the Team will not run. Revise the plan "
                "according to the feedback before proposing it again."
            )
            if self.feedback:
                text += f"\nFeedback: {escape_tag(self.feedback, 'user_decision')}"
        return f'<user_decision team_run_id="{self.team_run_id}">\n{text}\n</user_decision>'


class AgentResultBody(BaseModel):
    """委派出去的子 Agent 一次 Run 的结论；唤醒挂起的父 Agent，父 Run 已完成时也唤醒。

    子 Task 每次 Run 到终态投递一条（多次追加消息就有多条，逐条到达）；因父 Agent 停止而被取消的
    子 Run 不投递。不投影：委派卡片的状态已跟随子 Task 最新 Run。
    """

    model_config = ConfigDict(extra="forbid")
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = True
    WAKES_COMPLETED: ClassVar[bool] = True
    kind: Literal["agent_result"] = "agent_result"
    child_task_id: int
    status: str
    final_output: str | None = None
    end_reason: str | None = None

    def render(self) -> str:
        if self.final_output:
            content = escape_tag(self.final_output, "agent_result")
        else:
            content = f"(no final output; end_reason: {self.end_reason or 'unknown'})"
        return (
            f'<agent_result from="child_task:{self.child_task_id}" status="{self.status}">\n'
            f"{content}\n</agent_result>"
        )


class BackgroundResultBody(BaseModel):
    """后台任务（目前只有 Agent Team 执行）的结束结果；唤醒挂起的 Agent，已完成的 Task 也唤醒。"""

    model_config = ConfigDict(extra="forbid")
    DEFAULT_PROJECTED: ClassVar[bool] = False
    WAKES_SUSPENDED: ClassVar[bool] = True
    WAKES_COMPLETED: ClassVar[bool] = True
    kind: Literal["background_result"] = "background_result"
    job: Literal["agent_team"]
    job_id: int
    status: str
    content: str

    def render(self) -> str:
        content = escape_tag(self.content, "background_result")
        return (
            f'<background_result job="{self.job}" id="{self.job_id}" status="{self.status}">\n'
            f"{content}\n</background_result>"
        )


MessageBody = Annotated[
    UserInputBody | NoticeBody | DecisionBody | AgentResultBody | BackgroundResultBody,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class TaskMessage:
    """一条队列消息；``id`` 是外部队列行 id，也是排队与已消费阶段共用的消息身份。

    Run 内产生的提示只投进本次执行的 ``RunMailbox``，``id`` 为 ``None``、无需确认，随执行丢弃。
    """

    id: int | None
    body: MessageBody
    delivery: Delivery
    project_to_transport: bool

    @classmethod
    def of(
        cls,
        body: MessageBody,
        *,
        delivery: Delivery = "immediate",
        project_to_transport: bool | None = None,
        message_id: int | None = None,
    ) -> "TaskMessage":
        """按 body 类型补齐投影默认值。"""
        if project_to_transport is None:
            project_to_transport = type(body).DEFAULT_PROJECTED
        return cls(
            id=message_id, body=body, delivery=delivery, project_to_transport=project_to_transport
        )


def user_input_to_context(
    body: UserInputBody,
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """模型正文（图片用本地 locator，发请求前才解析，不落盘 base64）与展示绑定分开返回。"""
    message = body.message
    metadata = UserMessageMetadata(kind="user_input", presentation=message.presentation())
    if not message.image_paths:
        return ChatCompletionUserMessageParam(role="user", content=message.input_text), metadata
    content: list[ChatCompletionContentPartParam] = []
    if message.input_text:
        content.append({"type": "text", "text": message.input_text})
    content.extend(
        {"type": "image_url", "image_url": {"url": f"cosir-attachment://{path}"}}
        for path in message.image_paths
    )
    return ChatCompletionUserMessageParam(role="user", content=content), metadata


def tagged_to_context(
    body: DecisionBody | AgentResultBody | BackgroundResultBody,
) -> tuple[ChatCompletionUserMessageParam, UserMessageMetadata]:
    """带标签的系统消息：正文由 body 渲染，metadata 只记来源 kind。"""
    return (
        ChatCompletionUserMessageParam(role="user", content=body.render()),
        UserMessageMetadata(kind=body.kind),
    )


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
