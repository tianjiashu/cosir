"""外部队列读出的一批待处理消息，以及「从中取哪些、选哪一条」的规则。

能交接新 Run 的输入（``LAUNCH_INPUT_BODIES``：用户输入与 Agent 消息）都携带发起配置。一个新 Run
只消费**一条**这样的输入（见 :meth:`PendingMessages.next_input`），排队消息因此一个 Run 一条依次处理，
各用各自发送时的配置；其余种类的消息不受此限，新 Run 照常全部接收。用户输入按 ``delivery`` 分两条
通道：``immediate`` 是「现在就处理」（空闲时发送、排队消息被点了立即发送），``next_run`` 是排队（活动
Run 期间发送）；Agent 消息一律 ``immediate``。纯规则，不读写数据库。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.core.workflow.message_queue.session_message import LAUNCH_INPUT_BODIES, SessionMessage


@dataclass(frozen=True)
class PendingMessages:
    """按入队顺序的一批未确认消息（``SessionMessageQueue.pending`` 读出的那份）。"""

    messages: tuple[SessionMessage, ...]

    @classmethod
    def of(cls, messages: Iterable[SessionMessage]) -> PendingMessages:
        return cls(tuple(messages))

    def immediate(self) -> list[SessionMessage]:
        """当前驱动现在就该并入的：唤醒 / 续跑与运行中只投递这些，``next_run`` 留给下一个 Run。"""
        return [message for message in self.messages if message.delivery == "immediate"]

    def next_input(self) -> SessionMessage | None:
        """新 Run 要消费的那条输入（用户输入或 Agent 消息）：先取最早的 ``immediate``，没有再取最早
        的 ``next_run``。

        ``immediate`` 优先：取消后队列里留着旧的排队消息时，用户新发送（或点了立即发送）的那条先跑，
        旧排队消息等它完成后再依次接续。
        """
        inputs = [message for message in self.messages if isinstance(message.body, LAUNCH_INPUT_BODIES)]
        return next(
            (message for message in inputs if message.delivery == "immediate"),
            inputs[0] if inputs else None,
        )

    def initial(self, *, fresh_run: bool) -> list[SessionMessage]:
        """执行开始时投递的消息：新 Run 接收其余种类的全部消息与 :meth:`next_input` 选出的一条输入，
        其余输入留在队列里等后续 Run；唤醒 / 续跑只接收 :meth:`immediate`。
        """
        if not fresh_run:
            return self.immediate()
        selected = self.next_input()
        return [
            message
            for message in self.messages
            if not isinstance(message.body, LAUNCH_INPUT_BODIES) or message is selected
        ]
