"""Conversation Transport 的消息契约（Session 视角，扁平排列，以回合为单位）。"""

from typing import Literal

from typing_extensions import TypedDict

from app.assistant_transport.state.conversation_state_part import (
    ConversationStateMessageSource,
    ConversationStatePart,
)
from app.core.workflow.message_queue.session_message import Delivery
from app.models.run_record import RunError
from app.models.run_usage import RunUsage

MessageRole = Literal["user", "assistant"]


class ConversationStateOutcome(TypedDict):
    """Run 的结局：Run 进入终态时写到它最后一个可见回合的回复上，属于整个 Run、不拆到回合。

    前端看不到 Run，靠它显示历史回复的「已取消 / 失败 / 用量」；Run 被恢复时清空。
    ``contextWindowTotal`` 是该 Run 冻结的窗口上限，与 ``usage`` 同属一个 Run：上下文占用的
    「已用 / 上限」必须同源，两个 Run 之间换了模型也不会拿新窗口配旧用量。``durationMs`` 是 Run
    创建到终态的毫秒数，只从 Run 记录的时间戳推导（投影器不读墙钟），终态耗时必填。
    """

    status: str
    endReason: str | None
    error: RunError | None
    usage: RunUsage | None
    contextWindowTotal: int | None
    durationMs: int


class ConversationStateMessage(TypedDict):
    """一条 user / assistant 消息；一个回合是「开启它的输入（可没有）+ 一条回复」。

    Run 不在 wire 上：``turn`` 是消息所在的回合号（回复的 id 由它编码），``inherited`` 表示这条消息
    来自分叉继承的历史（没有本地 Run，不可交互、不可撤回）。``outcome`` 只在 Run 终态后挂在它最后
    一个可见回合的回复上，user 消息恒为 ``None``。``source`` 只有开启回合的 Agent 消息非空：它不是
    用户说的话，前端另行渲染、不可编辑；用户消息与助手消息恒为 ``None``。``parts`` 保留各自的流式与
    工具执行生命周期。
    """

    id: str
    role: MessageRole
    parts: list[ConversationStatePart]
    turn: int
    inherited: bool
    outcome: ConversationStateOutcome | None
    source: ConversationStateMessageSource | None


class ConversationQueuedMessage(TypedDict):
    """尚未被模型边界消费、需投影的排队消息；还没有回合。

    ``delivery`` 只供前端区分「下个模型边界并入」与「交给下一个 Run」，事实仍在外部队列。
    """

    id: str
    role: Literal["user"]
    parts: list[ConversationStatePart]
    delivery: Delivery
