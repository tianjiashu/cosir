"""Task fork 领域异常。"""


class TaskForkConflictError(RuntimeError):
    """Task fork 因源任务当前状态不可用而被拒绝。"""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # 源 Task 忙（Run 未落定、workspace 操作中）稍后可重试；源 Task 类型不支持 fork 则不可重试。
        self.retryable = retryable
