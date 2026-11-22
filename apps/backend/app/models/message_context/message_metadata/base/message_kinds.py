"""Session 消息队列的消息种类，以及按用途划分的两个子集。"""

from typing import Literal

MessageKind = Literal[
    "user_input",
    "agent_message",
    "notice",
    "decision",
    "background_result",
    "tool_image",
]
"""Session 消息队列的消息种类；并入 context 后记在 user 消息的 metadata 上，后端规则只读它。"""

CALL_RESULT_KINDS: frozenset[MessageKind] = frozenset(
    {"decision", "background_result", "tool_image"}
)
"""回答某次工具调用的消息种类：可带 ``tool_call_id``，归属那次调用而不是它所在的那一轮。"""

TURN_INPUT_KINDS: frozenset[MessageKind] = frozenset({"user_input", "agent_message"})
"""开启一轮的输入：投影成对话里的一条 user 消息，后面跟着它的回复。fork 切分、冷重建与消费投影
都按它认「一轮的起点」；只有其中的 ``user_input`` 可撤回、会触发标题支线。"""
