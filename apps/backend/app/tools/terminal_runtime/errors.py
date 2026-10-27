"""终端会话领域错误；观察策略错误不改变命令生命周期。"""


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


class TerminalRepeatedReadError(TerminalSessionError):
    """连续观察没有新信息；新输出或状态变化后可恢复，不终止程序。"""

    code = "TERMINAL_READ_UNCHANGED"
    retryable = True


class TerminalReadInProgressError(TerminalSessionError):
    """已有 read 正在等待；拒绝并发读取，不阻塞输入或信号。"""

    code = "TERMINAL_READ_IN_PROGRESS"
    retryable = True


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
    """Terminal Worker sidecar 不可用。"""

    code = "TERMINAL_WORKER_UNAVAILABLE"
    retryable = True


class TerminalWorkerProtocolError(TerminalWorkerUnavailableError):
    """Terminal Worker 控制帧格式无效。

    格式错误在当前进程中不可恢复，故不重试；继承父类以保证既有的
    ``except TerminalWorkerUnavailableError`` 捕获点仍能收敛 session 失败。
    """

    code = "TERMINAL_WORKER_PROTOCOL_ERROR"
    retryable = False


class TerminalWorkerHandshakeError(TerminalWorkerUnavailableError):
    """Worker 握手字段与当前启动实例不符，需修复 worker 配置后再重试。"""

    code = "TERMINAL_WORKER_HANDSHAKE_REJECTED"
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
