"""当前上下文窗口 token 占用的业务值对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextUsage:
    """当前上下文窗口的 token 占用快照（输入侧估算）。

    属性:
        used_tokens: 当前上下文消息累计占用的 token（字符估算，模型无关）。
        total_tokens: 实际上下文窗口上限（token）= min(模型最大窗口, 全局软上限)。
    """

    used_tokens: int
    total_tokens: int
