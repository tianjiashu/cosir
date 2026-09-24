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


class TerminalWorkerProtocolError(TerminalWorkerUnavailableError):
    """Terminal Worker 协议不兼容：握手字段或控制帧格式与 backend 期望不符。

    与父类的区别是**确定性**：同一对（backend 版本、worker 二进制）无论重试多少次都会失败
    （典型成因：sidecar 未随协议升级重建、可执行文件被替换成其它程序），因此标记
    ``retryable=False``，正确处置是重建/替换 worker 二进制，而不是重试工具调用。继承父类以保证
    既有的 ``except TerminalWorkerUnavailableError`` 捕获点（session 失败收敛）不遗漏。
    """

    code = "TERMINAL_WORKER_PROTOCOL_MISMATCH"
    retryable = False


class TerminalWorkerBackpressureError(TerminalSessionError):
    """Terminal Worker 输入队列已满。"""

    code = "TERMINAL_INPUT_BACKPRESSURE"
    retryable = True


class TerminalWorkerSignalUnsupportedError(TerminalSessionError):
    """Worker 明确拒绝当前平台或终端状态下的 signal。"""

    code = "TERMINAL_SIGNAL_UNSUPPORTED"


class TerminalWorkerSignalFailedError(TerminalSessionError):
    """Worker 已处理 signal 请求但无法完成实际投递。"""

    code = "TERMINAL_SIGNAL_FAILED"
