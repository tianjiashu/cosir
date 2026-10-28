"""终端会话领域错误；输入租约与等待条件错误不改变命令或会话生命周期。"""


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


class TerminalInputHeldByUserError(TerminalSessionError):
    """用户正在该会话输入，Agent 等待租约超时；用户空闲或手动释放后可重试，不影响会话。"""

    code = "TERMINAL_INPUT_HELD_BY_USER"
    retryable = True


class TerminalIntegrationRequiredError(TerminalSessionError):
    """等待条件依赖 shell 集成，而该会话没有可用集成；改用 output_quiet / match。"""

    code = "TERMINAL_INTEGRATION_REQUIRED"


class TerminalSessionCapacityError(TerminalSessionError):
    """达到全进程活跃会话上限，或该 Task 的 Agent 会话上限。"""

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

    格式错误在当前进程中不可恢复，故不重试；读线程捕获后补发 ``WORKER_DISCONNECTED`` 收敛会话。
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
