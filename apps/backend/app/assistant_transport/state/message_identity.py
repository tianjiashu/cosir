"""Transport 消息 id 的编码规则：投影、冷重建共用这一处。

- 用户消息：message_queue 行 id 的字符串（排队与已并入阶段 id 不变）；
- 用户消息之后的助手回复：``assistant-{用户消息 id}``；
- Run 内第一条用户消息之前的助手回复（唤醒、结果续跑等没有用户输入的 Run）：
  ``assistant-run-{runId}``。Run 号与消息行 id 是两个独立的整数序列，前缀不同才不会撞 id。
"""

from app.models.fork_reply import ForkReply

_REPLY_PREFIX = "assistant-"
_LEADING_PREFIX = "assistant-run-"


def user_message_id(message_id: int) -> str:
    return str(message_id)


def reply_id(message_id: int) -> str:
    return f"{_REPLY_PREFIX}{message_id}"


def leading_reply_id(run_id: int) -> str:
    return f"{_LEADING_PREFIX}{run_id}"


def parse_reply_id(value: str) -> ForkReply | None:
    """把回复的 wire id 解码成领域身份；不是回复 id 时返回 ``None``，由请求边界拒绝。"""

    leading = value.startswith(_LEADING_PREFIX)
    prefix = _LEADING_PREFIX if leading else _REPLY_PREFIX
    number = value.removeprefix(prefix)
    if not value.startswith(prefix) or not number.isascii() or not number.isdigit():
        return None
    if int(number) <= 0:
        return None
    return ForkReply(run_id=int(number)) if leading else ForkReply(input_message_id=int(number))
