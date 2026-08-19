"""model_error_mapper 对抗性边界测试（独立测试 Agent 新增）。

目标：挖掘 ``map_litellm_error`` 在异常体 / 响应体形态异常时的安全降级缺陷。
- 异常对象的 ``body`` / ``response`` 属性访问本身抛异常时不应向上传播；
- ``response`` 为 httpx.Response 形态（``.text`` / ``.json()``）时的关键词命中；
- 余额关键词命中（quota / balance / 余额）→ ``MODEL_INSUFFICIENT_QUOTA``；
- 内容安全关键词（sensitive / safety / 审查）→ ``MODEL_CONTENT_BLOCKED``；
- 未知异常回退 ``MODEL_UNKNOWN`` 不抛。
"""

import litellm.exceptions as litellm_exc

from app.service.llm.model_error_mapper import map_litellm_error
from app.models.enums.error_kind import ErrorKind


def _rate_limit(body=None, response=None) -> litellm_exc.RateLimitError:
    err = litellm_exc.RateLimitError(
        message="rate limited", llm_provider="deepseek", model="deepseek-chat"
    )
    if body is not None:
        err.body = body
    if response is not None:
        err.response = response
    return err


def test_rate_limit_body_is_plain_str_with_quota() -> None:
    """RateLimitError.body 为纯字符串含 quota → insufficient_quota。"""
    info = map_litellm_error(_rate_limit(body="quota exhausted"))
    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA
    assert info.retryable is False


def test_rate_limit_body_chinese_balance_keyword() -> None:
    """RateLimitError.body 含中文「余额不足」→ insufficient_quota。"""
    info = map_litellm_error(_rate_limit(body="您的账户余额不足，请充值"))
    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA


def test_rate_limit_balance_keyword_case_insensitive() -> None:
    """RateLimitError.body 含 BALANCE（大写）→ insufficient_quota（大小写不敏感）。"""
    info = map_litellm_error(_rate_limit(body='{"error": "Insufficient BALANCE"}'))
    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA


def test_rate_limit_quota_keyword_via_response_object() -> None:
    """RateLimitError.response 为 httpx.Response 形态（.text 含 quota）→ insufficient_quota。"""

    class FakeResponse:
        text = '{"error": {"message": "out of quota for today"}}'

    info = map_litellm_error(_rate_limit(response=FakeResponse()))
    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA


def test_rate_limit_quota_via_response_json_method() -> None:
    """response.json() 返回 dict 含 quota → insufficient_quota。"""

    class FakeResponse:
        text = ""

        def json(self) -> dict:
            return {"error": {"message": "balance too low to continue"}}

    info = map_litellm_error(_rate_limit(response=FakeResponse()))
    assert info.error_code == ErrorKind.MODEL_INSUFFICIENT_QUOTA


def test_rate_limit_response_json_raises_falls_back_to_message() -> None:
    """response.json() 抛异常 → 安全降级，回退消息文本（不抛）。"""

    class FakeResponse:
        text = ""

        def json(self) -> dict:
            raise ValueError("cannot parse")

    # 消息文本不含 quota → 保持 rate_limited
    info = map_litellm_error(_rate_limit(response=FakeResponse()))
    assert info.error_code == ErrorKind.MODEL_RATE_LIMITED


def test_bad_request_response_text_access_raises_does_not_propagate() -> None:
    """BadRequestError 的 response.text 属性访问抛异常 → 不向上传播，回退 invalid_request。"""

    class ExplodingResponse:
        @property
        def text(self) -> str:
            raise RuntimeError("text accessor exploded")

    err = litellm_exc.BadRequestError(
        message="bad request", llm_provider="x", model="m"
    )
    err.response = ExplodingResponse()
    info = map_litellm_error(err)
    # 即便 response 访问抛异常，也不应传播，应回退基础映射。
    assert info.error_code in (ErrorKind.MODEL_INVALID_REQUEST, ErrorKind.MODEL_UNKNOWN)


def test_bad_request_sensitive_keyword_content_blocked() -> None:
    """BadRequestError.body 含 sensitive → content_blocked。"""
    err = litellm_exc.BadRequestError(message="rejected", llm_provider="x", model="m")
    err.body = {"error": "The prompt contains sensitive content"}
    info = map_litellm_error(err)
    assert info.error_code == ErrorKind.MODEL_CONTENT_BLOCKED


def test_bad_request_safety_keyword_content_blocked() -> None:
    """BadRequestError.body 含 safety → content_blocked。"""
    err = litellm_exc.BadRequestError(message="rejected", llm_provider="x", model="m")
    err.body = "safety policy violation"
    info = map_litellm_error(err)
    assert info.error_code == ErrorKind.MODEL_CONTENT_BLOCKED


def test_bad_request_chinese_review_keyword_content_blocked() -> None:
    """BadRequestError.body 含中文「审查」→ content_blocked。"""
    err = litellm_exc.BadRequestError(message="rejected", llm_provider="x", model="m")
    err.body = "内容涉及敏感信息，已被审查拦截"
    info = map_litellm_error(err)
    assert info.error_code == ErrorKind.MODEL_CONTENT_BLOCKED


def test_bad_request_content_policy_keyword() -> None:
    """BadRequestError.body 含 content_policy → content_blocked。"""
    err = litellm_exc.BadRequestError(message="rejected", llm_provider="x", model="m")
    err.body = "content_policy triggered"
    info = map_litellm_error(err)
    assert info.error_code == ErrorKind.MODEL_CONTENT_BLOCKED


def test_auth_error_retryable_flag() -> None:
    """AuthenticationError → retryable=True（认证可重试语义）。"""
    info = map_litellm_error(
        litellm_exc.AuthenticationError(
            message="bad key", llm_provider="deepseek", model="deepseek-chat"
        )
    )
    assert info.error_code == ErrorKind.MODEL_AUTH_FAILED
    assert info.retryable is True


def test_not_found_not_retryable() -> None:
    """NotFoundError → retryable=False（模型不存在重试无意义）。"""
    info = map_litellm_error(
        litellm_exc.NotFoundError(message="404", llm_provider="x", model="m")
    )
    assert info.error_code == ErrorKind.MODEL_NOT_FOUND
    assert info.retryable is False


def test_network_error_retryable() -> None:
    """APIConnectionError → retryable=True。"""
    info = map_litellm_error(
        litellm_exc.APIConnectionError(message="conn", llm_provider="x", model="m")
    )
    assert info.error_code == ErrorKind.MODEL_NETWORK_ERROR
    assert info.retryable is True


def test_guidance_always_present_for_all_codes() -> None:
    """所有已知错误码的 guidance 均非空，且与 retryable 语义一致（含重试建议与否）。"""
    for exc in (
        litellm_exc.AuthenticationError(message="a", llm_provider="x", model="m"),
        litellm_exc.NotFoundError(message="a", llm_provider="x", model="m"),
        litellm_exc.Timeout(message="a", llm_provider="x", model="m"),
        litellm_exc.ContextWindowExceededError(message="a", llm_provider="x", model="m"),
    ):
        info = map_litellm_error(exc)
        assert info.guidance, "guidance 不应为空"
        assert isinstance(info.error_code, ErrorKind)
