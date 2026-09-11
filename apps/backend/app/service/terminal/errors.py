"""Terminal session domain errors."""


class TerminalSessionError(Exception):
    """终端 session 领域错误基类。"""

    code = "TERMINAL_SESSION_ERROR"
    retryable = False


class TerminalSessionNotFoundError(TerminalSessionError):
    """session 不存在。"""

    code = "TERMINAL_SESSION_NOT_FOUND"


class TerminalSessionOwnershipError(TerminalSessionError):
    """session 不属于当前 Task。"""

    code = "TERMINAL_SESSION_OWNERSHIP_ERROR"


class TerminalSessionStateError(TerminalSessionError):
    """当前 session 状态不允许请求的操作。"""

    code = "TERMINAL_SESSION_STATE_ERROR"


class TerminalSessionCapacityError(TerminalSessionError):
    """达到 backend 级 session 或 subscriber 容量上限。"""

    code = "TERMINAL_SESSION_CAPACITY"
    retryable = True


class TerminalSessionResyncRequiredError(TerminalSessionError):
    """请求 cursor 已早于 ring buffer 当前可用范围。"""

    code = "TERMINAL_RESYNC_REQUIRED"
    retryable = False

    def __init__(self, after_seq: int, first_available_seq: int, next_seq: int) -> None:
        super().__init__(
            f"output cursor {after_seq} is no longer available; "
            f"first available sequence is {first_available_seq}"
        )
        self.after_seq = after_seq
        self.first_available_seq = first_available_seq
        self.next_seq = next_seq


class TerminalWorkerUnavailableError(TerminalSessionError):
    """Terminal Worker sidecar 不可用或协议握手失败。"""

    code = "TERMINAL_WORKER_UNAVAILABLE"
    retryable = True


class TerminalWorkerBackpressureError(TerminalSessionError):
    """Terminal Worker 输入队列已满。"""

    code = "TERMINAL_INPUT_BACKPRESSURE"
    retryable = True
