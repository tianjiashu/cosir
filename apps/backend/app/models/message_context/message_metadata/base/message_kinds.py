"""消息种类的唯一词表：Session 消息队列的种类、落库 user 行的种类，以及按用途划分的两个子集。

两处同一个概念只用一个词：队列行的 ``kind`` 并入 context 后原样记在 user 行的 metadata 上。给模型看的
标签（``<system_notice>`` 等）是提示词协议，归各消息体的 ``TAG``，不跟这里的词走。
"""

from typing import Literal

SessionMessageKind = Literal[
    "user_input", "agent_message", "notice", "decision", "background_result"
]
"""入队的消息种类（``session_messages.kind`` 与消息体的判别值）。"""

MessageKind = Literal[SessionMessageKind, "tool_image"]
"""落库 user 行的种类：入队的种类，加上不入队、由 observe 节点直接写入的工具图片。后端规则只读它。"""

CALL_RESULT_KINDS: frozenset[MessageKind] = frozenset(
    {"decision", "background_result", "tool_image"}
)
"""回答某次工具调用的消息种类：可带 ``tool_call_id``，归属那次调用而不是它所在的那一轮。"""

TURN_INPUT_KINDS: frozenset[MessageKind] = frozenset({"user_input", "agent_message"})
"""开启一轮的输入：投影成对话里的一条 user 消息，后面跟着它的回复。fork 切分、冷重建与消费投影
都按它认「一轮的起点」；只有其中的 ``user_input`` 可撤回、会触发标题支线。"""
