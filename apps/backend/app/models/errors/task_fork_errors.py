"""Task fork 领域异常。"""


class TaskForkConflictError(RuntimeError):
    """Task fork 因源任务当前状态不可用而被拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SnapshotNotReadyError(RuntimeError):
    """源 Task snapshot 缺失或未通过 canonical schema 校验。"""
