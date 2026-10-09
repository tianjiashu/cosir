"""模型失败分类与响应消息提取的契约测试。

本模块锁定当前已定稿的两条对外契约（状态码不再参与分类，理由见 ``model_failure`` 模块
docstring：端点业务码与状态码并不统一，框架不推断业务类别）：

1. ``classify_model_failure`` 只按异常的**类型名**识别「没有响应体」的传输层失败（超时 / 连接），
   输出必须是 ``ErrorKind`` 的 model 分类值；其余一律返回 ``None``——不读状态码、不读异常文本、
   不猜 provider 业务错误。
2. ``extract_model_response_message`` 只从异常（含异常链）的响应体数据属性里取 provider 原始
   ``message``，读不到返回 ``None``，且绝不上抛。

另锁定两条文案契约：分类词表只有一处事实源（``ErrorKind``）；``run_failure_message`` 对每个可
产出的 code 都给出非空、provider 无关且不含状态码数字的中文文案。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.constant import Constant
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
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
    """只带 ``status_code`` 的替身：用于钉住「状态码不参与分类」。"""

    def __init__(self, status_code: object, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _BodyError(Exception):
    """带响应体数据属性的替身（OpenAI 兼容 SDK 形状）。"""

    def __init__(self, body: object, message: str = "") -> None:
        super().__init__(message)
        self.body = body


class _TimeoutLikeError(Exception):
    """异常类型名含 timeout 的替身（provider SDK 超时类的通用命名）。"""


class _ConnectionLikeError(Exception):
    """异常类型名含 connect 的替身（provider SDK 连接类的通用命名）。"""


def _flow_codes() -> list[str]:
    """返回 ``Constant.Run`` 声明的流程类失败 code（单一事实源）。"""

    return [
        value
        for name, value in vars(Constant.Run).items()
        if name.startswith("RUN_FAILURE_CODE_") and isinstance(value, str)
    ]


def _model_codes() -> list[str]:
    """返回 ``ErrorKind`` 中的模型调用错误分类值。"""

    return [member.value for member in ErrorKind if member.name.startswith("MODEL_")]


# --- classify_model_failure：只认传输层类型名 --------------------------------------------


def test_exception_type_name_carries_timeout_and_connection_semantics() -> None:
    """按异常类型名判定超时与网络不可达——这两类通常没有响应体。"""

    assert classify_model_failure(_TimeoutLikeError("boom")) == ErrorKind.MODEL_TIMEOUT.value
    assert (
        classify_model_failure(_ConnectionLikeError("boom")) == ErrorKind.MODEL_NETWORK_ERROR.value
    )
    assert classify_model_failure(TimeoutError("boom")) == ErrorKind.MODEL_TIMEOUT.value


def test_status_code_attributes_are_ignored() -> None:
    """状态码不参与分类：带状态码的异常同样返回 ``None``，业务类别交给响应体消息通道。"""

    for status_code in (400, 401, 402, 403, 404, 408, 413, 418, 429, 500, 503, 504, True):
        assert classify_model_failure(_StatusError(status_code)) is None


def test_error_text_is_never_guessed_into_a_business_kind() -> None:
    """异常文本不参与分类：报文体里的限流/余额字样不得被推断成业务类别。"""

    assert classify_model_failure(Exception("insufficient balance")) is None
    assert classify_model_failure(Exception("Rate limit reached for requests")) is None
    assert classify_model_failure(_StatusError(429, "insufficient balance")) is None


def test_unrecognized_exception_returns_none_instead_of_guessing() -> None:
    """判不出原因时返回 ``None``：由调用方决定兜底 code，分类器不猜异常来源。"""

    assert classify_model_failure(Exception("boom")) is None


def test_classification_result_is_always_a_usable_end_reason_identifier() -> None:
    """分类结果必须可直接写入 ``end_reason``（合法标识符）。"""

    samples: list[BaseException] = [
        _TimeoutLikeError("boom"),
        _ConnectionLikeError("boom"),
        TimeoutError("boom"),
        ConnectionError("boom"),
    ]

    for sample in samples:
        outcome = classify_model_failure(sample)
        assert outcome is not None
        assert outcome.isidentifier()


# --- extract_model_response_message：只取响应体里的原始 message --------------------------


def test_response_message_is_extracted_from_nested_error_body() -> None:
    """OpenAI 兼容形状：``body["error"]["message"]`` 必须原样取出（仅去首尾空白）。"""

    exc = _BodyError({"error": {"message": "  Insufficient Balance  ", "type": "quota"}})

    assert extract_model_response_message(exc) == "Insufficient Balance"


def test_response_message_reads_plain_and_json_bodies() -> None:
    """裸 ``{"message": ...}``、``msg`` 别名与 JSON 字符串响应体都要能提取。"""

    assert extract_model_response_message(_BodyError({"message": "bad request"})) == "bad request"
    assert extract_model_response_message(_BodyError({"error": {"msg": "boom"}})) == "boom"
    assert (
        extract_model_response_message(_BodyError('{"error": {"message": "quota exceeded"}}'))
        == "quota exceeded"
    )


def test_response_message_walks_the_exception_chain() -> None:
    """外层异常没有响应体时，沿异常链向 cause 查找。"""

    inner = _BodyError({"error": {"message": "model not found"}})
    outer = RuntimeError("wrapped provider failure")
    outer.__cause__ = inner

    assert extract_model_response_message(outer) == "model not found"


def test_response_message_is_none_without_readable_body() -> None:
    """没有响应体、响应体不是映射、或映射里没有 message 时返回 ``None``。"""

    assert extract_model_response_message(TimeoutError("boom")) is None
    assert extract_model_response_message(_BodyError("not json")) is None
    assert extract_model_response_message(_BodyError({"error": {"code": "x"}})) is None
    assert extract_model_response_message(_BodyError(None)) is None


def test_response_message_ignores_blank_and_non_string_messages() -> None:
    """空串、纯空白与非字符串 message 都不算有效消息。"""

    assert extract_model_response_message(_BodyError({"error": {"message": "   "}})) is None
    assert extract_model_response_message(_BodyError({"error": {"message": 42}})) is None


def test_response_message_never_raises_on_hostile_body() -> None:
    """响应体属性读取抛异常时返回 ``None``，不影响失败收尾。"""

    class _HostileBody:
        def __getitem__(self, _key: object) -> object:
            raise RuntimeError("hostile body")

    exc = _BodyError(_HostileBody())

    assert extract_model_response_message(exc) is None


# --- 文案目录 ---------------------------------------------------------------------------


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
        classify_model_failure(_TimeoutLikeError("boom")),
        classify_model_failure(_ConnectionLikeError("boom")),
    }
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
    assert run_failure_message(Constant.Run.RUN_FAILURE_CODE_UNKNOWN) == fallback
    assert run_failure_message("totally_unknown_code") == fallback


@pytest.mark.parametrize("body", [{"error": {"message": "x"}}, "{\"message\": \"x\"}"])
def test_response_message_shapes_stay_supported(body: object) -> None:
    """两种受支持形状（映射与 JSON 字符串）在参数化输入下都返回同一条消息。"""

    assert extract_model_response_message(_BodyError(body)) == "x"


def test_simple_namespace_body_is_not_a_mapping() -> None:
    """``SimpleNamespace`` 不是 ``Mapping``：按无响应体处理，不猜属性。"""

    exc = _BodyError(SimpleNamespace(message="should not be read"))

    assert extract_model_response_message(exc) is None
