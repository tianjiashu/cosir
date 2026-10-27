"""Agent Team 工具准备阶段的领域错误。

统一承载准备/持久化 TeamRun 时抛出的可诊断错误，并显式携带 ``retryable`` 标记；工具捕获后
据此决定写进模型文本的是「改参数再调」还是「让用户检查配置、不要原样重试」。
"""

from __future__ import annotations


class TeamToolError(Exception):
    """Agent Team 工具准备或持久化失败时抛出的领域错误。

    ``retryable`` 标记模型能否靠修正自己的参数解决：总目标为空、节点子目标与配置不符为
    ``True``；Team 配置本身无效、节点引用的 Agent 不可用等需要用户修正，默认 ``False``。

    参数:
        message: 人读的错误描述，会进入工具观察的 ``reason``，不得包含敏感信息。
        retryable: 模型修正参数后能否再次调用该工具，默认 ``False``。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
