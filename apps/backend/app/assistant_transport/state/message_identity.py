"""Transport 消息 id 的编码规则：投影、冷重建与 fork 定位共用这一处。

- 用户消息：message_queue 行 id 的字符串（排队与已并入阶段 id 不变）；
- 用户消息之后的助手回复：``assistant-{用户消息 id}``；
- Run 内第一条用户消息之前的助手回复（唤醒、结果续跑等没有用户输入的 Run）：
  ``assistant-run-{runId}``。Run 号与消息行 id 是两个独立的整数序列，前缀不同才不会撞 id。
"""

from dataclasses import dataclass

_REPLY_PREFIX = "assistant-"
_LEADING_PREFIX = "assistant-run-"


def user_message_id(message_id: int) -> str:
    return str(message_id)


def reply_id(message_id: int) -> str:
    return f"{_REPLY_PREFIX}{message_id}"


def leading_reply_id(run_id: int) -> str:
    return f"{_LEADING_PREFIX}{run_id}"


@dataclass(frozen=True)
class TurnRef:
    """一条 wire 消息所在的「一轮」：以某条用户消息开头，或是某个 Run 开头没有用户输入的回复。

    恰有一个字段非空。
    """

    user_message_id: int | None = None
    leading_run_id: int | None = None


def turn_of(wire_message_id: str) -> TurnRef:
    """把 wire 消息 id 解析为它所在的一轮；用户消息与它的回复属于同一轮。

    异常:
        ValueError: id 不符合上述编码规则。
    """

    if wire_message_id.startswith(_LEADING_PREFIX):
        return TurnRef(leading_run_id=_positive_int(wire_message_id[len(_LEADING_PREFIX) :]))
    if wire_message_id.startswith(_REPLY_PREFIX):
        return TurnRef(user_message_id=_positive_int(wire_message_id[len(_REPLY_PREFIX) :]))
    return TurnRef(user_message_id=_positive_int(wire_message_id))


def _positive_int(text: str) -> int:
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"invalid message id: {text!r}")
    return int(text)
