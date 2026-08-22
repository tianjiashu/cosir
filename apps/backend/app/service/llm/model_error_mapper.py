"""litellm 异常归一为模型错误码（设计文档阶段 4 ①）。

本模块是模型调用错误映射的唯一收口：把 litellm 抛出的一族异常子类映射为
稳定、可路由的 ``ErrorKind`` 错误码与面向用户的修复引导文案（英文，供前端
按 code 展示）。``ModelNotConfiguredError`` 属解析链异常，不在此映射范围
（已实现于 ``app/service/llm/model_resolver_service.py``，设计文档决议不
重复定义独立类）。

放置于 ``service/llm/``（领域错误映射纯函数，仅依赖 ``models/enums`` 与
litellm，不依赖任何编排层），使 ``core``（``runner``）引用为合法的
``core → service`` 方向、``service``（``provider_connection_test_service``）
引用为同层合法，避免 ``service → core`` 反向依赖。
"""

from dataclasses import dataclass

import litellm.exceptions as litellm_exc

from app.models.enums.error_kind import ErrorKind


@dataclass(frozen=True)
class ModelErrorInfo:
    """归一后的模型错误信息（设计文档阶段 4 ①）。

    属性:
        error_code: 稳定的错误码（``ErrorKind.MODEL_*``）。
        guidance: 面向模型的富文本修复引导（英文，含可重试建议）。
        retryable: 是否可**自动/立即**重试（``False`` 表示需人工处置，如充值、
            修改内容后手动重试）；须与 ``guidance`` 中重试建议一致。
    """

    error_code: ErrorKind
    guidance: str
    retryable: bool


#: 各 litellm 异常子类 → 错误码映射（不含需查 status_code/message 的余额不足分支）。
_EXCEPTION_CODE_MAP: tuple[tuple[type, ErrorKind], ...] = (
    (litellm_exc.AuthenticationError, ErrorKind.MODEL_AUTH_FAILED),
    (litellm_exc.NotFoundError, ErrorKind.MODEL_NOT_FOUND),
    (litellm_exc.APIConnectionError, ErrorKind.MODEL_NETWORK_ERROR),
    (litellm_exc.Timeout, ErrorKind.MODEL_NETWORK_ERROR),
    (litellm_exc.RateLimitError, ErrorKind.MODEL_RATE_LIMITED),
    (litellm_exc.ContextWindowExceededError, ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED),
    (litellm_exc.BadRequestError, ErrorKind.MODEL_INVALID_REQUEST),
)

#: 余额不足关键词（HTTP 429 响应体命中其一即判 insufficient_quota）。
_QUOTA_KEYWORDS: tuple[str, ...] = ("quota", "balance", "余额")


def _error_code_guidance(
    error_code: ErrorKind,
    *,
    retryable: bool,
) -> ModelErrorInfo:
    """按错误码生成默认的修复引导文案（英文）。

    参数:
        error_code: 归一后的错误码。
        retryable: 是否可自动/立即重试（不含人工处置后的重试）。

    返回:
        ``ModelErrorInfo``（含面向模型的中性引导文案）。

    异常:
        无。

    副作用:
        无。
    """

    guidance_by_code: dict[ErrorKind, str] = {
        ErrorKind.MODEL_AUTH_FAILED: (
            "Model authentication failed. Check the provider API key in the "
            "provider settings and try again."
        ),
        ErrorKind.MODEL_NOT_FOUND: (
            "Model not found. Verify the model name is correct and supported "
            "by the configured provider."
        ),
        ErrorKind.MODEL_NETWORK_ERROR: (
            "Network error while calling the model. Retry the turn or verify "
            "the proxy / network configuration."
        ),
        ErrorKind.MODEL_RATE_LIMITED: (
            "Rate limit exceeded. Wait a moment and retry, or lower "
            "concurrency."
        ),
        ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED: (
            "Request exceeded the model context window. Reduce the context "
            "or restart the task with a fresh turn."
        ),
        ErrorKind.MODEL_INVALID_REQUEST: (
            "The model rejected the request. Review the request parameters / "
            "tool schema and retry."
        ),
        ErrorKind.MODEL_INSUFFICIENT_QUOTA: (
            "Provider quota or balance is insufficient. Top up the account or "
            "switch to another provider, then retry."
        ),
        ErrorKind.MODEL_CONTENT_BLOCKED: (
            "The provider content-safety filter blocked the request. Rephrase "
            "the input and try again."
        ),
    }
    fallback = (
        "An unexpected model error occurred. Retry the turn; if it persists, "
        "check the backend logs."
    )
    return ModelErrorInfo(
        error_code=error_code,
        guidance=guidance_by_code.get(error_code, fallback),
        retryable=retryable,
    )


def map_litellm_error(exc: BaseException) -> ModelErrorInfo:
    """把 litellm 异常映射为归一化错误信息（设计文档阶段 4 ①）。

    依次判定：先按异常子类命中基础映射；命中 ``RateLimitError`` 或
    ``BadRequestError`` 时额外检查响应体是否含余额不足 / 内容审查关键词，
    命中则覆盖为更精确的错误码。未命中任何已知子类回退 ``MODEL_UNKNOWN``。

    参数:
        exc: 上游 ``model_node`` / runner / 连通性测试捕获的原始异常（litellm
            或任意 ``BaseException``）。

    返回:
        归一后的 ``ModelErrorInfo``（含 error_code / guidance / retryable）。

    异常:
        无（对所有输入安全降级，最差回退 ``MODEL_UNKNOWN``）。

    副作用:
        无。
    """

    for exc_type, code in _EXCEPTION_CODE_MAP:
        if isinstance(exc, exc_type):
            if code == ErrorKind.MODEL_RATE_LIMITED:
                body = _extract_error_body(exc)
                if _body_mentions_quota(body):
                    return _error_code_guidance(
                        ErrorKind.MODEL_INSUFFICIENT_QUOTA, retryable=False
                    )
                return _error_code_guidance(ErrorKind.MODEL_RATE_LIMITED, retryable=True)
            if code == ErrorKind.MODEL_INVALID_REQUEST:
                body = _extract_error_body(exc)
                if map_provider_content_blocked(body):
                    return _error_code_guidance(
                        ErrorKind.MODEL_CONTENT_BLOCKED, retryable=False
                    )
                return _error_code_guidance(ErrorKind.MODEL_INVALID_REQUEST, retryable=True)
            # 其余异常子类按基础映射返回。
            retryable = code in (
                ErrorKind.MODEL_NETWORK_ERROR,
                ErrorKind.MODEL_AUTH_FAILED,
            )
            return _error_code_guidance(code, retryable=retryable)
    return _error_code_guidance(ErrorKind.MODEL_UNKNOWN, retryable=True)


def _extract_error_body(exc: BaseException) -> str:
    """尽量从 litellm 异常中提取响应体文本（用于关键词判定）。

    litellm 异常可能暴露 ``body``（dict/str）或 ``response``；均缺失时回退
    到异常消息文本。本函数只做防御性读取，取不到返回空串。

    参数:
        exc: 捕获的异常。

    返回:
        归一后的响应体字符串；取不到则空串。

    异常:
        无（对缺失属性安全降级为空串）。

    副作用:
        无。
    """

    for attr in ("body", "response"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            rendered = _render_body_dict(value)
            if rendered:
                return rendered
        # httpx.Response 形态（litellm 常把响应包在 response 属性）：
        # 取已缓冲的 .text；未读取则尝试 .json()。
        text = _extract_response_text(value)
        if text:
            return text
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message and message != str(exc):
        return message
    rendered = str(exc)
    return rendered if rendered else ""


def _extract_response_text(value) -> str:
    """从 httpx.Response 中尽力提取响应体文本（用于关键词判定）。

    httpx.Response 在请求期已把响应体缓存于 ``.text``；未缓冲时尝试
    ``.json()``。两者都取不到返回空串。``.text`` / ``.json()`` 的访问与调用
    均包在 ``try/except`` 中，保证对任何对象形态安全降级（属性访问器本身
    抛异常也不向外传播，维护 ``map_litellm_error`` 永不抛的契约）。

    参数:
        value: ``response`` 属性的原始值（可能为 httpx.Response 或任意对象）。

    返回:
        响应体字符串；取不到则空串。

    异常:
        无（对缺失属性 / 属性访问抛异常 / 解析失败安全降级为空串）。

    副作用:
        无。
    """

    if value is None or not isinstance(value, object):
        return ""
    try:
        text = getattr(value, "text", None)
        if isinstance(text, str) and text:
            return text
    except Exception:
        return ""
    json_method = getattr(value, "json", None)
    if callable(json_method):
        try:
            data = json_method()
        except Exception:
            return ""
        if isinstance(data, str) and data:
            return data
        if isinstance(data, dict):
            return _render_body_dict(data)
    return ""


def _render_body_dict(body: dict) -> str:
    """把响应体 dict 渲染为用于关键词判定的扁平字符串。

    参数:
        body: 响应体 dict（可能含嵌套 ``error``）。

    返回:
        扁平字符串；body 为空返回空串。

    异常:
        无。

    副作用:
        无。
    """

    error_node = body.get("error")
    if isinstance(error_node, dict):
        rendered = str(error_node)
    elif isinstance(error_node, str):
        rendered = error_node
    else:
        rendered = str(body)
    return rendered


def _body_mentions_quota(body: str) -> bool:
    """判断响应体文本是否命中余额不足关键词。

    参数:
        body: 归一后的响应体字符串。

    返回:
        命中关键词（quota / balance / 余额）返回 True。

    异常:
        无。

    副作用:
        无。
    """

    lowered = body.lower()
    return any(kw.lower() in lowered for kw in _QUOTA_KEYWORDS)


#: 内容安全审查关键词（400 响应命中其一即判 content_blocked）。
_CONTENT_BLOCKED_KEYWORDS: tuple[str, ...] = (
    "datainspectionfailed",
    "content_filter",
    "content_policy",
    "safety",
    "不合法内容",
    "审查",
    "sensitive",
)


def map_provider_content_blocked(error_body: str) -> bool:
    """判断响应体文本是否命中内容安全审查码（设计文档阶段 4 ①）。

    400 响应体含 ``DataInspectionFailed`` 等审查码即判 ``content_blocked``；
    ``error_body`` 取不到时回退 ``invalid_request`` 语义（返回 False，不抛）。

    参数:
        error_body: 归一后的响应体字符串（可能为空）。

    返回:
        命中内容安全关键词返回 True；否则 False。

    异常:
        无。

    副作用:
        无。
    """

    if not error_body:
        return False
    lowered = error_body.lower()
    return any(kw in lowered for kw in _CONTENT_BLOCKED_KEYWORDS)
