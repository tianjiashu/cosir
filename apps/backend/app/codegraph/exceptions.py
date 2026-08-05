"""CodeGraph Kernel 通信与生命周期相关异常。

单一职责：定义 Kernel 子进程交互中可预期的结构化错误，使上层（第二阶段
LifecycleService / Agent 工具面）能按错误语义决定重试或降级，而非捕获裸
Exception。错误码与 agent-kernel 协议层 ``KernelErrorCode`` 一一对应。
"""

from app.codegraph.protocol import KernelErrorCode


class CodeGraphKernelError(Exception):
    """Kernel 交互的结构化错误基类。

    所有 Kernel 相关异常都携带 ``code``（协议错误码）与 ``retryable``（是否值得
    重试），方便上层做分类处理。

    职责边界：
        - 负责：承载错误码、可重试标记、可读消息。
        - 不负责：进程管理（归 Supervisor）、协议解析（归 Client）。
    """

    code: KernelErrorCode = KernelErrorCode.INTERNAL
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        """构造 Kernel 错误。

        参数:
            message: 人类可读的错误描述（英文，便于模型/日志消费）。
            retryable: 可选覆盖默认可重试标记；省略时沿用类属性。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class CodeGraphProtocolIncompatibleError(CodeGraphKernelError):
    """握手阶段协议版本不兼容。"""

    code = KernelErrorCode.PROTOCOL_INCOMPATIBLE
    retryable = False


class CodeGraphKernelUnavailableError(CodeGraphKernelError):
    """Kernel 进程不可用（未启动 / 已退出 / 内部致命）。"""

    code = KernelErrorCode.KERNEL_UNAVAILABLE
    retryable = True


class CodeGraphKernelTimeoutError(CodeGraphKernelError):
    """请求在限定时间内未收到响应。"""

    code = KernelErrorCode.TIMEOUT
    retryable = True


class CodeGraphWorkspaceNotIndexedError(CodeGraphKernelError):
    """workspace 未建索引（无 .workspace_event/ 或未初始化成功）。"""

    code = KernelErrorCode.WORKSPACE_NOT_INDEXED
    retryable = False


class CodeGraphToolNotAllowedError(CodeGraphKernelError):
    """工具被 CODEGRAPH_MCP_TOOLS 裁剪拒绝。"""

    code = KernelErrorCode.TOOL_NOT_ALLOWED
    retryable = False


class CodeGraphInvalidRequestError(CodeGraphKernelError):
    """请求形状非法。"""

    code = KernelErrorCode.INVALID_REQUEST
    retryable = False


class CodeGraphIndexingFailedError(CodeGraphKernelError):
    """索引写入（init/sync）执行失败。"""

    code = KernelErrorCode.INDEXING_FAILED
    retryable = True


class CodeGraphIndexLockedError(CodeGraphKernelError):
    """索引被占用（另一进程/实例持锁，含 SQLite 忙碌）。"""

    code = KernelErrorCode.INDEX_LOCKED
    retryable = True


class CodeGraphNodeMissingError(CodeGraphKernelError):
    """锁定的 node 运行时缺失（解析失败）。

    这是启动前的前置错误，不经由协议，故不绑定 KernelErrorCode；独立成类以便
    上层给出「安装/配置 node」的明确引导。
    """

    retryable = False


def error_from_code(code: KernelErrorCode, message: str, retryable: bool) -> CodeGraphKernelError:
    """把协议错误码映射为对应的 Python 异常实例。

    参数:
        code: 协议层返回的错误码。
        message: 协议层返回的错误消息。
        retryable: 协议层返回的可重试标记。

    返回:
        与 ``code`` 对应的 ``CodeGraphKernelError`` 子类实例。

    异常:
        无。

    副作用:
        无。
    """
    mapping: dict[KernelErrorCode, type[CodeGraphKernelError]] = {
        KernelErrorCode.PROTOCOL_INCOMPATIBLE: CodeGraphProtocolIncompatibleError,
        KernelErrorCode.KERNEL_UNAVAILABLE: CodeGraphKernelUnavailableError,
        KernelErrorCode.TIMEOUT: CodeGraphKernelTimeoutError,
        KernelErrorCode.WORKSPACE_NOT_INDEXED: CodeGraphWorkspaceNotIndexedError,
        KernelErrorCode.TOOL_NOT_ALLOWED: CodeGraphToolNotAllowedError,
        KernelErrorCode.INVALID_REQUEST: CodeGraphInvalidRequestError,
        KernelErrorCode.INDEXING_FAILED: CodeGraphIndexingFailedError,
        KernelErrorCode.INDEX_LOCKED: CodeGraphIndexLockedError,
        KernelErrorCode.INTERNAL: CodeGraphKernelError,
    }
    return mapping.get(code, CodeGraphKernelError)(message, retryable=retryable)
