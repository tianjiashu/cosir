"""Transport 消息 id 的编码规则：投影、冷重建与 fork 请求共用这一处。

- 用户消息：message_queue 行 id 的字符串（排队与已并入阶段 id 不变，撤回、立即发送、删除都按它
  寻址）；
- 回复：``assistant-turn-{回合号}``。每个回合只有一条回复，回合号在 Task 内唯一、撤回后不复用、fork
  时原样复制，所以同一条回复在来源与分支里 id 相同。前端把它当不透明值，只有 fork 请求在这里解码。
"""

_REPLY_PREFIX = "assistant-turn-"


def user_message_id(message_id: int) -> str:
    return str(message_id)


def reply_id(turn: int) -> str:
    return f"{_REPLY_PREFIX}{turn}"


def parse_reply_id(value: str) -> int | None:
    """把回复的 wire id 解码成回合号；不是回复 id 时返回 ``None``，由请求边界拒绝。

    回合号 0 是系统提示词，不是回复。
    """

    number = value.removeprefix(_REPLY_PREFIX)
    if number == value or not number.isascii() or not number.isdigit() or int(number) <= 0:
        return None
    return int(number)
