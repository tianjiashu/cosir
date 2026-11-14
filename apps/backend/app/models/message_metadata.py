"""消息正文之外的持久化契约；由 SDK 消息角色选择结构，不保存第二份角色或 Run 状态。"""

from typing import Literal

from openai.types import CompletionUsage
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.enums.tool_call_status import ToolCallEventStatus


class EmptyMessageMetadata(BaseModel):
    """system/developer/function 消息没有额外持久化信息。"""

    model_config = ConfigDict(extra="forbid")


MessageKind = Literal[
    "user_input",
    "agent_message",
    "notice",
    "decision",
    "background_result",
    "tool_result",
    "tool_image",
]
"""Session 消息队列的消息种类；并入 context 后记在 user 消息的 metadata 上，后端规则只读它。"""

CALL_RESULT_KINDS: frozenset[MessageKind] = frozenset(
    {"decision", "background_result", "tool_result", "tool_image"}
)
"""回答某次工具调用的消息种类：可带 ``tool_call_id``，归属那次调用而不是它所在的那一轮。"""

TURN_INPUT_KINDS: frozenset[MessageKind] = frozenset({"user_input", "agent_message"})
"""开启一轮的输入：投影成对话里的一条 user 消息，后面跟着它的回复。fork 切分、冷重建与消费投影
都按它认「一轮的起点」；只有其中的 ``user_input`` 可撤回、会触发标题支线。"""


class NoticeEntry(BaseModel):
    """合成进一条 ``<system_notice>`` 的单条通知：撤回回合时据此重新投递 Session 级通知，工具准入
    通知的推导据 ``banned_tools`` 对照上次告知的禁用集合，未了结事项通知据 ``open_items`` 对照上次
    告知的清单（见 ``NOTICE_LEVELS``）。"""

    model_config = ConfigDict(extra="forbid")
    notice_kind: str
    content: str
    banned_tools: list[str] | None = None
    open_items: list[str] | None = None


class UserMessageMetadata(BaseModel):
    """落库的 user 消息都来自 Session 消息队列：``kind`` 记录来源，一轮的输入另带展示绑定。

    所有队列消息进模型时都是 user 角色，区分来源只能靠 ``kind``，不解析正文标签：编辑资格、
    排队与消费投影都据此判断。模型正文与展示正文是同一份规范化胶囊文本（Skill 已补 ``path``），
    图片路径单独保存在 ``presentation``；snapshot 重建、编辑复用与 fork 都只从它取。Agent 消息
    （``agent_message``）的 ``presentation`` 只有纯文本正文，另带发送方 ``source``。内部注入
    的 user 指令（如结构化输出）只拼进请求副本、不落库。

    ``tool_call_id`` 记录调用结果类消息（见 :data:`CALL_RESULT_KINDS`）回答的是哪次调用：决定与
    后台结论可能在发起调用之后的回合才并入，撤回据它判断这行回答的是哪次调用（见
    ``turn_withdrawal_service``）。不对应某次调用的后台结论（重启结论）为 ``None``。

    ``notices`` 只用于合成通知行（``kind="notice"``）：记下合成进来的每条通知。系统提示词增量、
    子 Agent 目录变更这类环境通知只投递一次，所在的一轮被撤回时要据此重新投递，否则之后的 Run
    永远看不到这次变化。
    """

    model_config = ConfigDict(extra="forbid")
    kind: MessageKind
    presentation: UserInputPresentation | None = None
    source: MessageSource | None = None
    tool_call_id: str | None = None
    notices: list[NoticeEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _presentation_only_for_turn_inputs(self) -> "UserMessageMetadata":
        if (self.kind in TURN_INPUT_KINDS) != (self.presentation is not None):
            raise ValueError("presentation is required for turn inputs and forbidden otherwise")
        if (self.kind == "agent_message") != (self.source is not None):
            raise ValueError("source is required for agent_message and forbidden otherwise")
        if self.tool_call_id is not None and self.kind not in CALL_RESULT_KINDS:
            raise ValueError("tool_call_id is only allowed for call result messages")
        if self.notices and self.kind != "notice":
            raise ValueError("notices are only allowed for notice messages")
        return self


class AssistantMessageMetadata(BaseModel):
    """单次模型响应的观测信息，不进入后续模型请求；完成原因保留 Provider 原值。"""

    model_config = ConfigDict(extra="forbid")
    completion_id: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    usage: CompletionUsage | None = None


BackgroundKind = Literal["terminal_command"]


class BackgroundWork(BaseModel):
    """一次后台工作的持久化记录，挂在这次调用的后台占位行上，看板与卡片都从它重建。

    为什么不单独建表：后台工作就是对话里的一次工具调用，占位行已经随对话 fork、撤回、Session
    删除；记录挂在它上面，看板就是对话的一个投影，没有第二个事实源要手工同步。工作是否还在跑
    不在这里（进程内 ``command_results`` 的活条目才是），这里只记重启后还需要的事实。

    时间都是 UTC ISO 字符串，取自工作本身而不是行的时间戳：fork 复制行时会重新生成时间戳。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: BackgroundKind
    # 启动工作的 Run（只是来历：工作属于 Session，生命周期不看它）。
    run_id: int | None
    # 启动时的调用意图快照；UI 动词、图标与本地化仍归前端。
    label: str
    # 登记后台结果的时刻（转到后台的调用是它开始前台等待的时刻）。
    started_at: str
    # 结论到达、被撤销或被重启中断的时刻；还在等结论时为 ``None``。
    ended_at: str | None = None


class ToolMessageMetadata(BaseModel):
    """工具结果的展示事实与等待状态：``pending`` 的等待占位就是「等你操作」（见 :attr:`awaiting_decision`），
    其余字段是卡片重建所需载荷。"""

    model_config = ConfigDict(extra="forbid")
    status: ToolCallEventStatus
    display_data: dict[str, object] | None = None
    error: str | None = None
    # 后台结果的后台占位（见 ``ToolObservation.background``）：存在即后台占位，卡片状态与看板条目
    # 的持久化事实源。
    background: BackgroundWork | None = None

    @property
    def awaiting_decision(self) -> bool:
        """决定入口与展示投影共用的等待条件。"""
        return self.status == "pending" and self.background is None


MessageMetadata = (
    EmptyMessageMetadata | UserMessageMetadata | AssistantMessageMetadata | ToolMessageMetadata
)


def parse_message_metadata(role: str, value: object) -> MessageMetadata:
    """持久化边界按消息角色校验 JSON；未知字段或角色错配直接失败，不读取旧形状。"""
    metadata_type: type[MessageMetadata]
    if role == "user":
        metadata_type = UserMessageMetadata
    elif role == "assistant":
        metadata_type = AssistantMessageMetadata
    elif role == "tool":
        metadata_type = ToolMessageMetadata
    elif role in {"system", "developer", "function"}:
        metadata_type = EmptyMessageMetadata
    else:
        raise ValueError(f"unsupported message role: {role}")
    return metadata_type.model_validate(value)


def empty_message_metadata(role: str) -> MessageMetadata:
    """创建没有附属信息的消息；工具结果必须由调用方提供明确展示状态。"""
    return parse_message_metadata(role, {})
