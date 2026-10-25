"""Agent Team 工具准备阶段的领域错误。

统一承载准备/持久化 TeamRun 时抛出的可诊断错误，并显式携带 ``retryable`` 标记；工具捕获后
把它传给 ``tool_error``，决定写进模型文本的是「可以重试」还是「不要重试」提示。这样工具层能用
单一异常类型区分「配置/输入类不可重试」与「瞬时写入失败」两类错误，而不是一律兜底为可重试。
"""

from __future__ import annotations


class TeamToolError(Exception):
    """Agent Team 工具准备或持久化失败时抛出的领域错误。

    配置/输入校验失败与数据库瞬时写入失败都通过该异常向上传播，由工具层统一捕获并
    映射为失败观察。``retryable`` 标记错误是否值得模型修正后重试：本项目当前两类错误
    均不可重试（配置类需人工修正，DB 类不鼓励盲目重试），因此默认 ``False``。

    参数:
        message: 人读的错误描述，会进入工具观察的 ``reason``，不得包含敏感信息。
        retryable: 模型修正参数/状态后能否再次调用该工具，默认 ``False``。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
