"""Langfuse 可观测性接入测试（配置门禁与降级路径）。

覆盖技术方案第八章测试计划第 1-2 项：未启用 / 缺密钥 / 未安装 langfuse 时，``tracing_enabled``
返回 False、``turn_trace`` 产出空 callbacks、``flush_langfuse`` 安全空操作，且**绝不触碰真实
上报**或中断主流程。所有用例不依赖真实 langfuse 后端。
"""

import logging
import sys

import pytest

from app.config.settings import Settings
from app.core.observability.langfuse_tracing import (
    TraceMetadata,
    flush_langfuse,
    tracing_enabled,
    turn_trace,
)


def _metadata() -> TraceMetadata:
    """构造一条最小可观测元数据用于 ``turn_trace`` 测试。

    参数:
        无。

    返回:
        含必要标识的 ``TraceMetadata``。
    """

    return TraceMetadata(
        task_id="task-1",
        turn_id="turn-1",
        agent_id="agent-1",
        workspace_id="ws-1",
    )


def test_tracing_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """未显式开启时 ``tracing_enabled`` 必须返回 False（零开销路径）。"""

    monkeypatch.setattr(Settings, "LANGFUSE_ENABLED", False)
    monkeypatch.setattr(Settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(Settings, "LANGFUSE_SECRET_KEY", "sk")
    assert tracing_enabled() is False


def test_tracing_disabled_when_keys_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """开启但缺密钥时降级为 False 并记一次 warning（不触碰上报）。"""

    monkeypatch.setattr(Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(Settings, "LANGFUSE_PUBLIC_KEY", None)
    monkeypatch.setattr(Settings, "LANGFUSE_SECRET_KEY", None)

    with caplog.at_level(logging.WARNING, logger="coding_agent.backend"):
        assert tracing_enabled() is False

    assert any(r.message == "langfuse_disabled_missing_keys" for r in caplog.records)


def test_tracing_disabled_when_langfuse_not_installed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """开启且密钥齐备但 langfuse 不可导入时降级为 False 并记 warning。"""

    monkeypatch.setattr(Settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(Settings, "LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setattr(Settings, "LANGFUSE_SECRET_KEY", "sk")
    # 设成 None 使 ``import langfuse`` 抛出 ImportError（标准 import 机制行为）。
    monkeypatch.setitem(sys.modules, "langfuse", None)

    with caplog.at_level(logging.WARNING, logger="coding_agent.backend"):
        assert tracing_enabled() is False

    assert any(r.message == "langfuse_not_installed" for r in caplog.records)


def test_turn_trace_yields_empty_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未启用时 ``turn_trace`` 必须产出空列表，且不创建任何 Langfuse 客户端。"""

    monkeypatch.setattr(Settings, "LANGFUSE_ENABLED", False)
    with turn_trace(_metadata()) as callbacks:
        assert callbacks == []


def test_flush_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """未启用时 ``flush_langfuse`` 必须安全返回（不创建客户端、不抛异常）。"""

    monkeypatch.setattr(Settings, "LANGFUSE_ENABLED", False)
    flush_langfuse()  # 仅断言不抛异常


def test_langfuse_client_uses_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造 Langfuse 客户端时必须显式传入 base_url（项目用 CODING_AGENT_ 前缀，绕过默认读取）。"""

    captured: dict = {}

    class _RecordingClient:
        def __init__(self, public_key, secret_key, base_url):
            captured["public_key"] = public_key
            captured["secret_key"] = secret_key
            captured["base_url"] = base_url

    class _FakeLangfuse:
        def __new__(cls, *args, **kwargs):
            return _RecordingClient(**kwargs)

    langfuse = pytest.importorskip("langfuse")
    monkeypatch.setattr(langfuse, "Langfuse", _FakeLangfuse)
    monkeypatch.setattr(Settings, "LANGFUSE_PUBLIC_KEY", "pk-x")
    monkeypatch.setattr(Settings, "LANGFUSE_SECRET_KEY", "sk-x")
    monkeypatch.setattr(Settings, "LANGFUSE_BASE_URL", "https://lf.example.com")

    from app.core.observability.langfuse_tracing import _build_langfuse_client

    client = _build_langfuse_client()
    assert isinstance(client, _RecordingClient)
    assert captured["public_key"] == "pk-x"
    assert captured["secret_key"] == "sk-x"  # noqa: S105
    assert captured["base_url"] == "https://lf.example.com"
