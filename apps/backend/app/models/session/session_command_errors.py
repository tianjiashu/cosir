"""用户对 Session 发出的命令与 Session 当前状态冲突时的领域异常。"""


class SessionCommandConflictError(ValueError):
    """当前 Session 状态不允许本次用户命令；``code`` 是稳定的机器码，API 映射为 409。

    ``retryable`` 表示「刷新状态后重试可能成功」，前端据此决定是否提供重新同步入口；冲突已成定局
    （如排队消息已被并入）时为 ``False``。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


__all__ = ["SessionCommandConflictError"]
