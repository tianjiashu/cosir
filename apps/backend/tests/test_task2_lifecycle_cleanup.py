"""Lifecycle cleanup regressions for the Child Agent runtime boundary."""

from __future__ import annotations

import pytest

import app.lifespan as lifespan_module


@pytest.mark.asyncio
async def test_startup_failure_closes_initialized_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动失败先停 actor，再关闭服务依赖（终端服务随依赖关闭，lifespan 不单独关）。"""

    calls: list[str] = []

    class _Spaces:
        async def aclose(self) -> None:
            calls.append("spaces_aclose")

    monkeypatch.setattr(lifespan_module, "task_runtime_spaces", _Spaces())
    monkeypatch.setattr(
        lifespan_module,
        "close_service_dependencies",
        lambda: calls.append("close_dependencies"),
    )

    await lifespan_module._cleanup_startup_failure()

    assert calls == ["spaces_aclose", "close_dependencies"]
