"""模型错误码归一（model_error_mapper）单元测试。

覆盖设计文档阶段 4 的映射契约：
- 各 litellm 异常子类 → 稳定 ``ErrorKind`` 错误码 / 可重试标记 / 引导文案；
- ``RateLimitError`` 响应体含余额关键词（quota / balance / 余额）→ 升级为
  ``MODEL_INSUFFICIENT_QUOTA``；
- ``BadRequestError`` 响应体含内容安全审查码 → ``MODEL_CONTENT_BLOCKED``；
- 未知异常回退 ``MODEL_UNKNOWN``（不抛）；
- ``map_provider_content_blocked`` 空体回退 False（不抛）。
"""

import litellm.exceptions as litellm_exc

from app.service.llm.model_error_mapper import (
    map_litellm_error,
    map_provider_content_blocked,
)
from app.models.enums.error_kind import ErrorKind


def _auth_error() -> litellm_exc.AuthenticationError:
    return litellm_exc.AuthenticationError(
        message="invalid api key", llm_provider="deepseek", model="deepseek-chat"
    )


def test_authentication_error_maps_to_auth_failed() -> None:
    """AuthenticationError → MODEL_AUTH_FAILED（可重试）。"""
    info = map_litellm_error(_auth_error())

    assert info.error_code == ErrorKind.MODEL_AUTH_FAILED
    assert info.retryable is True
    assert "api key" in info.guidance.lower()


def test_rate_limit_maps_to_rate_limited() -> None:
    """RateLimitError（无余额关键词）→ MODEL_RATE_LIMITED（可重试）。"""
    err = litellm_exc.RateLimitError(
        message="rate limited",
        llm_provider="deepseek",
        model="deepseek-chat",
    )
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_RATE_LIMITED
    assert info.retryable is True


def test_rate_limit_with_quota_keyword_maps_to_insufficient_quota() -> None:
    """RateLimitError 响应体含 quota/余额 → MODEL_INSUFFICIENT_QUOTA（不可重试）。"""
    err = litellm_exc.RateLimitError(
        message="rate limited",
        llm_provider="deepseek",
        model="deepseek-chat",
    )
    # litellm 会把响应体解析结果挂到 .body；模拟余额不足的关键词响应体
    err.body = {"error": "quota exhausted for the current billing cycle"}
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA
    assert info.retryable is False


def test_context_window_exceeded_maps_to_context_window_exceeded() -> None:
    """ContextWindowExceededError → MODEL_CONTEXT_WINDOW_EXCEEDED。"""
    err = litellm_exc.ContextWindowExceededError(
        message="exceeds max tokens", llm_provider="deepseek", model="deepseek-chat"
    )
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED
    assert info.retryable is False


def test_bad_request_maps_to_invalid_request() -> None:
    """BadRequestError（无审查关键词）→ MODEL_INVALID_REQUEST。"""
    err = litellm_exc.BadRequestError(
        message="bad request", llm_provider="deepseek", model="deepseek-chat"
    )
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_INVALID_REQUEST
    assert info.retryable is True


def test_bad_request_with_content_blocked_maps_to_content_blocked() -> None:
    """BadRequestError 响应体含 DataInspectionFailed 审查码 → MODEL_CONTENT_BLOCKED。"""
    err = litellm_exc.BadRequestError(
        message="content blocked",
        llm_provider="x",
        model="m",
    )
    err.body = {"error": {"message": "DataInspectionFailed: content rejected"}}
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_CONTENT_BLOCKED
    assert info.retryable is False


def test_api_connection_error_maps_to_network() -> None:
    """APIConnectionError → MODEL_NETWORK_ERROR（可重试）。"""
    err = litellm_exc.APIConnectionError(
        message="connection failed", llm_provider="deepseek", model="deepseek-chat"
    )
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_NETWORK_ERROR
    assert info.retryable is True


def test_timeout_maps_to_network() -> None:
    """Timeout → MODEL_NETWORK_ERROR（可重试）。"""
    err = litellm_exc.Timeout(message="timed out", llm_provider="x", model="m")
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_NETWORK_ERROR
    assert info.retryable is True


def test_not_found_maps_to_model_not_found() -> None:
    """NotFoundError → MODEL_NOT_FOUND。"""
    err = litellm_exc.NotFoundError(
        message="model not found", llm_provider="deepseek", model="deepseek-chat"
    )
    info = map_litellm_error(err)

    assert info.error_code == ErrorKind.MODEL_NOT_FOUND
    assert info.retryable is False


def test_unknown_exception_maps_to_model_unknown() -> None:
    """未知异常回退 MODEL_UNKNOWN（不抛，可重试兜底）。"""
    info = map_litellm_error(ValueError("unexpected"))

    assert info.error_code == ErrorKind.MODEL_UNKNOWN
    assert info.retryable is True


def test_map_provider_content_blocked_returns_false_on_empty_body() -> None:
    """空响应体回退 invalid_request 语义（返回 False，不抛）。"""
    assert map_provider_content_blocked("") is False


def test_map_provider_content_blocked_hits_keyword() -> None:
    """响应体含审查关键词返回 True。"""
    assert map_provider_content_blocked('{"message":"DataInspectionFailed"}') is True
    assert map_provider_content_blocked("content_filter triggered") is True
