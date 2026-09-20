"""模型失败分类与 Run 失败文案目录的契约测试。

本模块锁定三条对外契约：

1. ``classify_model_failure`` 只按异常的**通用形状**（HTTP 状态码、异常类型名、错误文本里的
   通用语义词）归类，输出必须是 ``ErrorKind`` 的 model 分类值；无法判定时返回 ``None``，而不是
   猜测来源或抛出异常。
2. 分类词表只有一处事实源：模型错误 code 一律取 ``ErrorKind``，本模块不另行定义字面量。
3. ``run_failure_message`` 对每个可产出的 code 都给出**非空且 provider 无关**的中文文案：不含
   任何厂商/模型名、不含状态码数字，未知 code 一律回退通用文案。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.llm_provider.model_failure import classify_model_failure
from app.models import conversation_run_failure as failure_catalog
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.error_kind import ErrorKind

# 文案里绝不允许出现的厂商/模型词（大小写不敏感比较）。
_PROVIDER_TOKENS = (
    "deepseek",
    "openai",
    "anthropic",
    "gemini",
    "claude",
    "gpt",
    "qwen",
    "moonshot",
    "groq",
    "firecrawl",
)


class _StatusError(Exception):
    """等价于 provider SDK 状态异常的替身：只带 ``status_code``。"""

    def __init__(self, status_code: object, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _ResponseStatusError(Exception):
    """只把状态码挂在 ``response`` 上的替身，覆盖鸭子类型读取的第二条路径。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


class _TimeoutLikeError(Exception):
    """异常类型名含 timeout 的替身（provider SDK 超时类的通用命名）。"""


class _ConnectionLikeError(Exception):
    """异常类型名含 connect 的替身（provider SDK 连接类的通用命名）。"""


def _flow_codes() -> list[str]:
    """返回目录模块声明的流程类失败 code。"""

    return [
        value
        for name, value in vars(failure_catalog).items()
        if name.startswith("RUN_FAILURE_CODE_") and isinstance(value, str)
    ]


def _model_codes() -> list[str]:
    """返回 ``ErrorKind`` 中的模型调用错误分类值。"""

    return [member.value for member in ErrorKind if member.name.startswith("MODEL_")]


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (401, ErrorKind.MODEL_AUTH_FAILED),
        (403, ErrorKind.MODEL_AUTH_FAILED),
        (402, ErrorKind.MODEL_INSUFFICIENT_QUOTA),
        (404, ErrorKind.MODEL_NOT_FOUND),
        (408, ErrorKind.MODEL_TIMEOUT),
        (413, ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED),
        (429, ErrorKind.MODEL_RATE_LIMITED),
        (500, ErrorKind.MODEL_SERVICE_ERROR),
        (503, ErrorKind.MODEL_SERVICE_ERROR),
        (504, ErrorKind.MODEL_TIMEOUT),
        (400, ErrorKind.MODEL_INVALID_REQUEST),
        (418, ErrorKind.MODEL_INVALID_REQUEST),
    ],
)
def test_status_code_is_classified_into_error_kind(
    status_code: int, expected_kind: ErrorKind
) -> None:
    """状态码是首选判据：4xx 分档、5xx 统一服务端故障，输出取 ``ErrorKind``。"""

    assert classify_model_failure(_StatusError(status_code)) == expected_kind.value


def test_status_code_is_read_from_response_when_missing_at_top_level() -> None:
    """顶层没有 ``status_code`` 时回退读 ``response.status_code``。"""

    assert (
        classify_model_failure(_ResponseStatusError(429)) == ErrorKind.MODEL_RATE_LIMITED.value
    )


def test_boolean_status_code_is_not_treated_as_http_status() -> None:
    """``bool`` 是 ``int`` 的子类，但不得被当作状态码（否则 1xx 判定会误命中）。"""

    assert classify_model_failure(_StatusError(True)) is None


def test_exception_type_name_carries_timeout_and_connection_semantics() -> None:
    """顶层无语义状态码时，按异常类型名判定超时与网络不可达。"""

    assert (
        classify_model_failure(_TimeoutLikeError("boom")) == ErrorKind.MODEL_TIMEOUT.value
    )
    assert (
        classify_model_failure(_ConnectionLikeError("boom"))
        == ErrorKind.MODEL_NETWORK_ERROR.value
    )
    assert classify_model_failure(TimeoutError("boom")) == ErrorKind.MODEL_TIMEOUT.value


@pytest.mark.parametrize(
    ("message", "expected_kind"),
    [
        ("Error code: 402 - Insufficient Balance", ErrorKind.MODEL_INSUFFICIENT_QUOTA),
        ("you exceeded your current quota", ErrorKind.MODEL_INSUFFICIENT_QUOTA),
        ("Rate limit reached for requests", ErrorKind.MODEL_RATE_LIMITED),
        ("Too Many Requests", ErrorKind.MODEL_RATE_LIMITED),
        ("invalid api key provided", ErrorKind.MODEL_AUTH_FAILED),
        ("Unauthorized", ErrorKind.MODEL_AUTH_FAILED),
        ("no such model: foo", ErrorKind.MODEL_NOT_FOUND),
        ("maximum context length is 128000 tokens", ErrorKind.MODEL_CONTEXT_WINDOW_EXCEEDED),
        ("content filter triggered", ErrorKind.MODEL_CONTENT_BLOCKED),
        ("request timed out", ErrorKind.MODEL_TIMEOUT),
    ],
)
def test_error_text_keywords_are_used_as_last_resort(
    message: str, expected_kind: ErrorKind
) -> None:
    """文本语义只作兜底，且只匹配通用计费/限流/鉴权/模型/上下文/审核语义词。"""

    assert classify_model_failure(Exception(message)) == expected_kind.value


def test_status_code_wins_over_error_text() -> None:
    """状态码优先：429 的报文里即使出现余额字样，也应归类为限流。"""

    assert (
        classify_model_failure(_StatusError(429, "insufficient balance"))
        == ErrorKind.MODEL_RATE_LIMITED.value
    )


def test_unrecognized_exception_returns_none_instead_of_guessing() -> None:
    """判不出原因时返回 ``None``：由调用方决定兜底 code，分类器不猜异常来源。"""

    assert classify_model_failure(Exception("boom")) is None


def test_classification_result_is_always_a_usable_end_reason_identifier() -> None:
    """分类结果必须可直接写入 ``end_reason``（合法标识符）。"""

    samples: list[BaseException] = [
        _StatusError(402),
        _StatusError(503),
        _ResponseStatusError(404),
        _TimeoutLikeError("boom"),
        Exception("insufficient balance"),
    ]
    for sample in samples:
        outcome = classify_model_failure(sample)
        assert outcome is not None
        assert outcome.isidentifier()


def test_failure_codes_are_unique_identifiers_with_message() -> None:
    """流程码与 ``ErrorKind`` 模型码都是唯一标识符，且有非空文案。"""

    codes = _flow_codes() + _model_codes()
    assert codes
    assert len(codes) == len(set(codes))
    for code in codes:
        assert code.isidentifier()
        assert run_failure_message(code).strip()


def test_every_classifiable_code_has_its_own_message() -> None:
    """分类器可能产出的每个 code 都必须有专属文案，不得落到通用兜底。"""

    fallback = run_failure_message(None)
    produced = {
        classify_model_failure(_StatusError(status_code))
        for status_code in (400, 401, 402, 403, 404, 408, 413, 418, 429, 500, 503, 504)
    }
    produced.update(
        {
            classify_model_failure(_TimeoutLikeError("boom")),
            classify_model_failure(_ConnectionLikeError("boom")),
            classify_model_failure(Exception("content filter triggered")),
        }
    )
    produced.discard(None)
    assert produced
    for code in produced:
        assert run_failure_message(code) != fallback


def test_user_messages_are_provider_agnostic() -> None:
    """用户文案不得出现厂商/模型名，也不得泄漏 HTTP 状态码。"""

    for code in _flow_codes() + _model_codes():
        message = run_failure_message(code)
        lowered = message.lower()
        assert not any(token in lowered for token in _PROVIDER_TOKENS), code
        assert not any(character.isdigit() for character in message), code


def test_unknown_code_falls_back_to_generic_message() -> None:
    """``None`` 与未登记 code 都回退通用文案，保证 UI 永远有可展示文本。"""

    fallback = run_failure_message(None)
    assert fallback.strip()
    assert run_failure_message(failure_catalog.RUN_FAILURE_CODE_UNKNOWN) == fallback
    assert run_failure_message("totally_unknown_code") == fallback
