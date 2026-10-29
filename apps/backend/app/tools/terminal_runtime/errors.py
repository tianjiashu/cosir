"""终端会话领域错误；输入仲裁与等待条件错误不改变命令或会话生命周期。"""


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


class TerminalUserTypingError(TerminalSessionError):
    """用户一直在该会话打字，Agent 等不到空闲；用户停手几秒后可重试，不影响会话。"""

    code = "TERMINAL_USER_TYPING"
    retryable = True


class TerminalUserCommandRunningError(TerminalSessionError):
    """会话前台正运行用户启动的命令：Agent 输入会落进用户的程序，立即拒绝、不等待。

    不可重试：命令可能长驻（dev server、编辑器、ssh），等待只会让 Agent 卡住；
    应改用别的会话或询问用户。
    """

    code = "TERMINAL_USER_COMMAND_RUNNING"


class TerminalIntegrationRequiredError(TerminalSessionError):
    """等待条件依赖 shell 集成，而该会话没有可用集成；改用 output_quiet / match。"""

    code = "TERMINAL_INTEGRATION_REQUIRED"


class ShellResolutionError(TerminalSessionError):
    """shell 参数无法解析（未知名称、可执行文件不存在或当前平台不支持）。"""

    code = "TERMINAL_SHELL_UNAVAILABLE"


class TerminalSessionCapacityError(TerminalSessionError):
    """达到全进程活跃会话上限，或该 Task 的 Agent 会话上限。"""

    code = "TERMINAL_SESSION_CAPACITY"
    retryable = True


class TerminalSessionResyncRequiredError(TerminalSessionError):
    """请求 cursor 已早于 ring buffer 当前可用范围。"""

    code = "TERMINAL_RESYNC_REQUIRED"

    def __init__(self, after_seq: int, first_available_seq: int) -> None:
        super().__init__(
            f"output cursor {after_seq} is no longer available; "
            f"first available sequence is {first_available_seq}"
        )
        # API 据此构造 ``resync_required`` 帧（序号窗口由流缓冲在锁内现取）。
        self.after_seq = after_seq


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
