"""Conversation Transport 的途中 Agent 消息 part。"""

from typing import Literal

from typing_extensions import TypedDict


class ConversationStateReport(TypedDict):
    """派生 Task 的汇报：它的 Run 以什么结局落定，以及这是不是中间结果（之后还会再汇报）。"""

    status: Literal["completed", "failed"]
    intermediate: bool


class ConversationStateMessageSource(TypedDict):
    """Agent 消息的发送方：前端据 ``taskId`` 跳到来源 Task；``name`` 是发送时的显示名；``report``
    非空时这条消息是来源的汇报（见领域 ``MessageSource``）。"""

    taskId: int
    name: str
    report: ConversationStateReport | None


class ConversationStateAgentMessagePart(TypedDict):
    """回合途中到达的 Agent 消息：是当前回复过程里的一段，不开新回合。

    在这个 Run 还没有可见内容时到达的 Agent 消息才开回合（规则只在
    ``RuntimeMessageContextManager._opens_turn``），那时它是带 ``source`` 的 user 消息。两种形态
    表达的事实不同（开启回合 / 途中到达），展示上共用同一行。``id`` 是入队消息 id，与 user
    消息同一编码，消费投影据它去重。
    """

    type: Literal["agent-message"]
    id: str
    text: str
    source: ConversationStateMessageSource
