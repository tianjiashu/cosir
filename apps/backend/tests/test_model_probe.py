"""``probe_chat_model`` 的探活契约测试。

单一职责：只验证探活入口自身——成功判据（首个 chunk 即通过并关闭流）、失败分类（0 chunk /
鉴权 / 端点不存在 / 请求被拒 / 限流 / 上游错误 / 网络 / 超时 / 未物化设置），以及失败细节
不泄漏原始异常文本。不覆盖 service 层的结果映射（见 ``test_model_config_contract.py``）。
"""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider import model_probe
from app.core.llm_provider.model_probe import ModelProbeFailureKind, probe_chat_model

_MATERIALIZED = ModelSettings(
    base_url="https://example.test/v1",
    api_key="sk-test",
    model_name="demo-model",
    context_window_k=64,
    supports_thinking=False,
    supports_reasoning_effort=False,
    supports_image=False,
)


class _FakeStream:
    """最小异步流替身：可产出 chunk、抛异常或永久挂起，并记录是否被关闭。"""

    def __init__(
        self,
        chunks: tuple[str, ...] = (),
        error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._error = error
        self._hang = hang
        self.closed = False

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> str:
        if self._hang:
            await asyncio.sleep(60)
        if self._error is not None:
            raise self._error
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self) -> None:
        self.closed = True


class _FakeModel:
    """最小模型替身：``astream`` 返回预设流。"""

    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream

    def astream(self, messages: object, **kwargs: object) -> _FakeStream:
        return self.stream


def _install_model(monkeypatch: pytest.MonkeyPatch, stream: _FakeStream) -> None:
    """把探活使用的模型构建入口替换为返回给定流的替身。"""

    model = _FakeModel(stream)
    monkeypatch.setattr(model_probe, "build_chat_model", lambda settings: model)


def _status_error(status: int, *, message: str = "boom") -> openai.APIStatusError:
    """构造带状态码的 openai 错误（与真实 SDK 异常同族）。"""

    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError(message, response=response, body=None)


@pytest.mark.asyncio
async def test_probe_succeeds_on_first_chunk_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个生成 chunk 即视为可用，且必须关闭流。潜在缺陷：悬挂连接或消费整段生成。"""

    stream = _FakeStream(chunks=("chunk-1", "chunk-2"))
    _install_model(monkeypatch, stream)

    outcome = await probe_chat_model(_MATERIALIZED)

    assert outcome.ok is True
    assert outcome.failure_kind is None
    assert outcome.detail == ""
    assert stream.closed is True


@pytest.mark.asyncio
async def test_probe_reports_invalid_response_when_stream_has_no_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端点有响应但流里没有 chunk（如 Base URL 打到返回 HTML 的站点）必须判失败。

    潜在缺陷（历史事故）：只看 HTTP 200 就判连接成功，运行期却因无 chunk 直接失败。
    """

    _install_model(monkeypatch, _FakeStream())

    outcome = await probe_chat_model(_MATERIALIZED)

    assert outcome.ok is False
    assert outcome.failure_kind is ModelProbeFailureKind.INVALID_RESPONSE
    assert outcome.detail == "stream produced no chunk"


@pytest.mark.asyncio
async def test_probe_classifies_http_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """401/403/404/400/429/5xx 必须映射到稳定分类。潜在缺陷：所有失败都归为一个 code。"""

    cases = {
        401: ModelProbeFailureKind.AUTH_FAILED,
        403: ModelProbeFailureKind.AUTH_FAILED,
        404: ModelProbeFailureKind.ENDPOINT_NOT_FOUND,
        400: ModelProbeFailureKind.REQUEST_REJECTED,
        429: ModelProbeFailureKind.RATE_LIMITED,
        503: ModelProbeFailureKind.UPSTREAM_ERROR,
    }
    for status, expected in cases.items():
        _install_model(monkeypatch, _FakeStream(error=_status_error(status)))
        outcome = await probe_chat_model(_MATERIALIZED)
        assert outcome.ok is False
        assert outcome.failure_kind is expected, status
        assert outcome.detail == f"http_status={status}"


@pytest.mark.asyncio
async def test_probe_never_leaks_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """失败细节只含类型与状态码，不得回显异常正文。潜在缺陷：Key 或响应片段进入日志/文案。"""

    _install_model(
        monkeypatch,
        _FakeStream(error=_status_error(401, message="key sk-secret-leak is invalid")),
    )

    outcome = await probe_chat_model(_MATERIALIZED)

    assert outcome.failure_kind is ModelProbeFailureKind.AUTH_FAILED
    assert "sk-secret-leak" not in outcome.detail


@pytest.mark.asyncio
async def test_probe_classifies_network_and_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接失败与不可解析响应分别归类。潜在缺陷：都落到 unknown 导致无法给出排查方向。"""

    _install_model(monkeypatch, _FakeStream(error=httpx.ConnectError("connect failed")))
    network = await probe_chat_model(_MATERIALIZED)
    assert network.failure_kind is ModelProbeFailureKind.NETWORK_ERROR

    _install_model(
        monkeypatch,
        _FakeStream(error=ValueError("No generation chunks were returned")),
    )
    invalid = await probe_chat_model(_MATERIALIZED)
    assert invalid.failure_kind is ModelProbeFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_probe_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """整体探测受 timeout 约束。潜在缺陷：端点不回包时无限等待。"""

    _install_model(monkeypatch, _FakeStream(hang=True))

    outcome = await probe_chat_model(_MATERIALIZED, timeout_seconds=0.05)

    assert outcome.ok is False
    assert outcome.failure_kind is ModelProbeFailureKind.TIMEOUT


@pytest.mark.asyncio
async def test_probe_rejects_unmaterialized_settings_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未物化设置必须直接判 invalid_config，且不得构建模型或发起请求。"""

    built: list[object] = []
    monkeypatch.setattr(
        model_probe,
        "build_chat_model",
        lambda settings: built.append(settings),
    )

    outcome = await probe_chat_model(ModelSettings(model_name="demo-model"))

    assert outcome.ok is False
    assert outcome.failure_kind is ModelProbeFailureKind.INVALID_CONFIG
    assert built == []
