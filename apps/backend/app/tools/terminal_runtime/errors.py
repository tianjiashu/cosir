"""终端会话领域错误；Agent 填入被拒与等待条件错误不改变命令或会话生命周期。

``advice`` 是投影给模型的下一步动作（英文，模型可见），``message`` 只说明发生了什么，两者不重复
（同一动作不在 message 与 advice 各写一遍）：需要不同动作时由抛出点给出不同的 message，动作本身
按错误类型声明一次。
"""


class TerminalSessionError(Exception):
    """终端 session 领域错误基类。"""

    code = "TERMINAL_SESSION_ERROR"
    retryable = False
    # 模型可见的下一步动作；None 表示 message 已自足，不追加建议。
    advice: str | None = None


class TerminalSessionNotFoundError(TerminalSessionError):
    """session 不存在。"""

    code = "TERMINAL_SESSION_NOT_FOUND"
    advice = (
        "It ended earlier or the backend restarted; call terminal_list to see the user's "
        "terminals."
    )


class TerminalSessionOwnershipError(TerminalSessionError):
    """session 不属于当前 Task。"""

    code = "TERMINAL_SESSION_OWNERSHIP_ERROR"


class TerminalSessionStateError(TerminalSessionError):
    """当前 session 状态不允许请求的操作（会话已结束、服务正在关闭）；message 已把原因写清，
    不再给类型级动作。"""

    code = "TERMINAL_SESSION_STATE_ERROR"


class TerminalPrefillRejectedError(TerminalSessionError):
    """当前不满足「把一行命令填进空提示符」的条件（无 shell 集成、不在提示符、提示符已有输入）。

    不可重试到同一提示符：用户敲了半行或已经填过一次时，Agent 应把命令写进回复里，或等下一个
    提示符；原因由 message 写清。
    """

    code = "TERMINAL_PREFILL_REJECTED"
    advice = (
        "Give the command in your reply instead, or ask the user to clear the prompt and try "
        "again later."
    )


class TerminalIntegrationRequiredError(TerminalSessionError):
    """等待条件依赖 shell 集成，而该会话没有可用集成；改用 output_quiet / match。"""

    code = "TERMINAL_INTEGRATION_REQUIRED"
    advice = "Use wait until=output_quiet or until=match in this session."


class ShellResolutionError(TerminalSessionError):
    """shell 参数无法解析（未知名称、可执行文件不存在或当前平台不支持）。"""

    code = "TERMINAL_SHELL_UNAVAILABLE"
    advice = (
        "Use a shell name from the shell parameter's list, or the path to an installed "
        "shell executable."
    )


class TerminalSessionCapacityError(TerminalSessionError):
    """达到全进程活跃会话上限。"""

    code = "TERMINAL_SESSION_CAPACITY"
    retryable = True
    advice = "Close a terminal in the terminal panel, then retry."


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
    advice = "Retry shortly; the session itself is unchanged."


class TerminalWorkerProtocolError(TerminalWorkerUnavailableError):
    """Terminal Worker 控制帧格式无效。

    格式错误在当前进程中不可恢复，故不重试；读线程捕获后补发 ``disconnected`` 收敛会话。
    """

    code = "TERMINAL_WORKER_PROTOCOL_ERROR"
    retryable = False
    advice = "Tell the user; starting a new session may work."


class TerminalWorkerHandshakeError(TerminalWorkerUnavailableError):
    """Worker 握手字段与当前启动实例不符，需修复 worker 配置后再重试。"""

    code = "TERMINAL_WORKER_HANDSHAKE_REJECTED"
    retryable = False
    advice = "Tell the user; the worker configuration needs fixing before retrying."


class TerminalWorkerBackpressureError(TerminalSessionError):
    """Terminal Worker 输入队列已满。"""

    code = "TERMINAL_INPUT_BACKPRESSURE"
    retryable = True
    advice = "The program is not reading input yet; retry once it does."

