from typing import Protocol

from app.models import RuntimeMessage


class ContextCompressor(Protocol):
    """上下文压缩器协议（预留扩展点，暂不实现具体算法）。

    为未来 context compaction 能力预留统一接口：实现方接收当前消息列表，
    返回压缩后的消息列表。具体压缩策略（摘要 / 裁剪 / 分层）由实现方决定，
    本类不提供默认实现，避免为臆想需求提前写死算法（YAGNI）。

    方法:
        compact: 将输入消息列表压缩为等价但更短的列表。
    """

    def compact(self, messages: list[RuntimeMessage]) -> list[RuntimeMessage]:
        """将消息列表压缩为更短的等价列表。

        参数:
            messages: 待压缩的消息列表。

        返回:
            压缩后的消息列表。
        """
        ...
