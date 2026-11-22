"""待并入的一批消息会怎样改变「等用户决定」的调用：并入之前就要知道答案的地方共用。

规则属于等待决定的过期（作废时机与告知在 ``decision_expiry``），这里只回答「这批消息并入后，哪些调用
不再等待、该不该作废」，纯规则、不写库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from app.core.workflow.message_queue.session_message import DecisionBody, SessionMessage


@dataclass(frozen=True)
class IncomingDecisionEffects:
    """字段:
        user_input: 这批消息里的第一条用户输入（没有为 ``None``）；有它，等用户回应的等待就要作废。
        answered_before_input: 决定排在 ``user_input`` 前面的调用——先作答、后发消息，回答算数。
        answered: 这批消息里有决定的全部调用；并入之后它们都不再等待（被回答，或已作废而决定被丢弃）。
    """

    user_input: SessionMessage | None
    answered_before_input: frozenset[str]
    answered: frozenset[str]

    @classmethod
    def of(cls, pending: list[SessionMessage]) -> IncomingDecisionEffects:
        """按出队顺序读这批消息（``RunMailbox.ordered``）；决定与输入同优先级、按到达先后并入，所以
        先后有意义。

        只有用户自己的输入算「说了别的话」。别的 Agent 发来的消息（子 Agent 汇报、会话间消息）同样
        会开启回合，但与用户答不答无关：让它作废提问，卡片就会在用户没操作时消失。
        """
        index = next((i for i, message in enumerate(pending) if message.kind == "user_input"), None)
        return cls(
            user_input=None if index is None else pending[index],
            answered_before_input=cls._decided(pending if index is None else pending[:index]),
            answered=cls._decided(pending),
        )

    def still_awaiting(self, tool_call_id: str) -> bool:
        """这次并入之后，这个此刻还在等待的调用是否仍在等：没被回答，也没有用户输入让它作废。"""
        return tool_call_id not in self.answered and self.user_input is None

    @staticmethod
    def _decided(messages: list[SessionMessage]) -> frozenset[str]:
        return frozenset(
            cast(DecisionBody, message.body).tool_call_id
            for message in messages
            if message.kind == "decision"
        )
