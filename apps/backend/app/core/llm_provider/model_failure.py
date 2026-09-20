"""把模型调用异常归类为稳定的 ``ErrorKind`` 分类值。

单一职责：读取一个异常实例的形状（HTTP 状态码、异常类型名、错误文本语义），返回
``app.models.enums.error_kind.ErrorKind`` 中对应的**模型调用错误**分类值。它不生成用户文案、
不落库、不发事件、不记日志。

不负责：
- 决定「这次失败该不该终止 run」——那是 workflow 的职责；
- 把分类值转成面向用户的文案——文案目录在 ``conversation_run_failure``；
- provider 连通性探测与重试策略——那是 provider service 与模型工厂的职责。

分类词表复用 ``ErrorKind``（日志、运行时事件与 Run 终态错误共用同一套稳定值），本模块**不**
另行定义分类字面量。

分类判据按可靠性从高到低依次尝试：

1. HTTP 状态码（``status_code`` 或 ``response.status_code``，鸭子类型读取，不 import 任何
   provider SDK，避免被 SDK 版本迁移绑死）；
2. 异常类型名中的语义词（``timeout`` / ``connect``）；
3. 错误文本里的通用计费、限流、鉴权、模型缺失、上下文、内容审核语义词。

三档都无法判定时返回 ``None``（表示「这不是一个可识别的模型调用错误」），由调用方决定兜底
code——本模块不猜测异常来源，也不抛出异常：分类失败不得让失败收尾链路本身再失败。
"""

from __future__ import annotations

from app.models.enums.error_kind import ErrorKind

# 状态码 → 分类：仅覆盖语义明确的档位，其余按 5xx / 4xx 分档。
_STATUS_KINDS: dict[int, ErrorKind] = {
    401: ErrorKind.MODEL_AUTH_FAILED,
    402: ErrorKind.MODEL_INSUFFICIENT_QUOTA,
    403: ErrorKind.MODEL_AUTH_FAILED,
    404: ErrorKind.MODEL_NOT_FOUND,
    408: ErrorKind.MODEL_TIMEOUT,
    413: ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED,
    429: ErrorKind.MODEL_RATE_LIMITED,
    504: ErrorKind.MODEL_TIMEOUT,
}

# 异常类型名语义词 → 分类。只匹配通用技术词，不匹配任何厂商名。
_EXCEPTION_NAME_KINDS: tuple[tuple[str, ErrorKind], ...] = (
    ("timeout", ErrorKind.MODEL_TIMEOUT),
    ("connect", ErrorKind.MODEL_NETWORK_ERROR),
)

# 错误文本语义词 → 分类，按优先级排列（计费问题对用户最可操作，排在最前）。
_MESSAGE_KEYWORD_KINDS: tuple[tuple[tuple[str, ...], ErrorKind], ...] = (
    (
        ("insufficient", "quota", "balance", "credit", "billing", "out of funds"),
        ErrorKind.MODEL_INSUFFICIENT_QUOTA,
    ),
    (
        ("rate limit", "too many requests"),
        ErrorKind.MODEL_RATE_LIMITED,
    ),
    (
        ("api key", "unauthorized", "authentication", "permission"),
        ErrorKind.MODEL_AUTH_FAILED,
    ),
    (
        ("no such model", "model not found", "model does not exist"),
        ErrorKind.MODEL_NOT_FOUND,
    ),
    (
        ("content filter", "content policy", "content_filter", "flagged"),
        ErrorKind.MODEL_CONTENT_BLOCKED,
    ),
    (
        ("context length", "maximum context", "context window", "too many tokens"),
        ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED,
    ),
    (
        ("timed out", "timeout"),
        ErrorKind.MODEL_TIMEOUT,
    ),
)


def _http_status_code(exc: BaseException) -> int | None:
    """读取异常携带的 HTTP 状态码；无可用或读取失败时返回 ``None``。

    先读 ``status_code``（openai / httpx 系 SDK 的通用形状），再回退 ``response.status_code``。
    只接受真正的 ``int``（排除 ``bool``），避免把标志位误当状态码。

    属性读取本身也可能抛异常（例如把 ``status_code`` 实现为会抛错的 property，或
    ``response`` 是会在访问时炸开的惰性对象），因此整段读取按「读不到就当没有」处理：分类是
    失败收尾链路的一环，绝不能因为异常对象自身的属性访问把收尾再打断一次。
    """

    try:
        candidate: object = getattr(exc, "status_code", None)
        if not isinstance(candidate, int) or isinstance(candidate, bool):
            candidate = getattr(getattr(exc, "response", None), "status_code", None)
    except Exception:  # 属性访问失败按「没有状态码」处理
        return None
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return candidate
    return None


def _kind_from_status_code(status_code: int) -> ErrorKind:
    """按 HTTP 状态码归类：先查精确表，再按 5xx / 4xx 分档。"""

    if status_code in _STATUS_KINDS:
        return _STATUS_KINDS[status_code]
    if status_code >= 500:
        return ErrorKind.MODEL_SERVICE_ERROR
    return ErrorKind.MODEL_INVALID_REQUEST


def classify_model_failure(exc: BaseException) -> str | None:
    """把模型调用异常归类为稳定的 ``ErrorKind`` 值。

    参数:
        exc: 模型调用链路上抛出的异常。它的具体类型不作要求——状态码与类型名按鸭子类型
            读取，因此未安装或未升级任何 provider SDK 也能分类。

    返回:
        ``ErrorKind`` 中 model 分类的稳定字符串值（可直接写入
        ``conversation_runs.end_reason`` 与 ``ConversationRunError.code``）；三档判据都无法命中时
        返回 ``None``，表示「无法识别为模型调用错误」，由调用方决定兜底 code。

    异常:
        无。分类过程本身不抛出——异常的 ``str()`` 或属性访问若为空/异常，一律按无法判定处理。

    副作用:
        无（纯函数，不写日志、不访问网络、不读数据库）。
    """

    status_code = _http_status_code(exc)
    if status_code is not None and 100 <= status_code < 600:
        return _kind_from_status_code(status_code).value

    exception_name = type(exc).__name__.lower()
    for keyword, kind in _EXCEPTION_NAME_KINDS:
        if keyword in exception_name:
            return kind.value

    try:
        message = str(exc).lower()
    except Exception:  # 分类兜底：异常自身不可字符串化时按无法判定处理
        message = ""
    for keywords, kind in _MESSAGE_KEYWORD_KINDS:
        if any(keyword in message for keyword in keywords):
            return kind.value

    return None


__all__ = [
    "classify_model_failure",
]
