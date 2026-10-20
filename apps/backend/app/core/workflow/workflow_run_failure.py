"""工作流运行期已知业务失败异常。"""

from __future__ import annotations


class WorkflowRunFailure(Exception):
    """工作流已知业务失败，携带稳定错误码交由 Runtime 落定失败状态。"""

    def __init__(self, failure_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code
