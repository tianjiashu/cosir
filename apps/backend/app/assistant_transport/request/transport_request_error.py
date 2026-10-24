"""纯 wire 契约校验失败的结构化异常。"""


class TransportRequestError(Exception):
    """由请求模型在解析阶段抛出，仅描述与领域持久化无关的结构性约束违反。

    API 适配层（``app/api/middleware/transport_error.py`` 的全局 exception_handler）负责把它
    映射为统一的 Assistant Transport HTTP 错误体，保持与 ``_raise_transport_error`` 相同的
    ``code`` / ``message`` / ``retryable`` 形状，使前端 HTTP 错误契约（``lib/http/errors.ts`` 的
    ``StructuredHttpError``）零改动。注意它**不是** Transport snapshot 的
    ``ConversationStateError`` 契约（后者只有 ``code`` / ``message``）。
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        """构造结构化请求校验异常。

        参数:
            status_code: 映射后的 HTTP 状态码。
            code: 稳定的机器可读错误码，与后端错误体 ``error.code`` 对齐。
            message: 面向用户的安全提示，不含密钥或异常堆栈。
            retryable: 客户端是否可在修正条件后重试。

        返回:
            无。

        异常:
            无。

        副作用:
            无；仅初始化不可变错误上下文。
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
