"""当前上下文窗口 token 占用的业务值对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextUsage:
    """当前上下文窗口的 token 占用快照（输入侧估算）。

    属性:
        used_tokens: 当前上下文消息累计占用的 token（字符估算，模型无关）。
        total_tokens: 实际上下文窗口上限（token）= min(模型最大窗口, 全局软上限)。

    约束:
        构造时即校验 ``used_tokens >= 0`` 与 ``total_tokens > 0``。负占用在物理上不可能
        （基于字符计数估算），属上游计量器缺陷；``__post_init__`` 在值对象层统一拦截，
        避免非法占用静默进入事件与持久化（服务层仍有防御性 clamp 兜底，但契约以此处为准）。
    """

    used_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        """构造时校验 token 占用合法性，非法值立即抛 ``ValueError``。

        参数:
            无（校验实例字段）。

        返回:
            无。

        异常:
            ValueError: ``used_tokens < 0``（负占用不可能发生）或 ``total_tokens <= 0``
                （窗口上限必须为正）。

        副作用:
            无。
        """
        if self.used_tokens < 0:
            raise ValueError(
                f"used_tokens 必须 >= 0，收到负值 {self.used_tokens}（占用基于字符计数，不应为负）"
            )
        if self.total_tokens <= 0:
            raise ValueError(f"total_tokens 必须 > 0，收到 {self.total_tokens}")
