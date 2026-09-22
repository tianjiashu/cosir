"""Lifecycle cleanup regressions for the Child Agent runtime boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.lifespan as lifespan_module


class _CachedGetter:
    def __init__(self, value: object, calls: list[str], name: str) -> None:
        self.value = value
        self.calls = calls
        self.name = name

    def cache_info(self) -> SimpleNamespace:
        return SimpleNamespace(currsize=1)

    def __call__(self) -> object:
        self.calls.append(self.name)
        return self.value


@pytest.mark.asyncio
async def test_startup_failure_closes_initialized_runtime_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Executor:
        async def close(self) -> None:
            calls.append("executor_close")

    class _Terminal:
        def shutdown(self) -> None:
            calls.append("terminal_shutdown")

    monkeypatch.setattr(
        lifespan_module,
        "get_conversation_run_executor",
        _CachedGetter(_Executor(), calls, "get_executor"),
    )
    monkeypatch.setattr(
        lifespan_module,
        "get_child_agent_session_service",
        _CachedGetter(object(), calls, "get_child_service"),
    )
    monkeypatch.setattr(
        lifespan_module,
        "get_terminal_session_service",
        _CachedGetter(_Terminal(), calls, "get_terminal"),
    )
    monkeypatch.setattr(
        lifespan_module,
        "close_service_dependencies",
        lambda: calls.append("close_dependencies"),
    )

    await lifespan_module._cleanup_startup_failure()

    assert calls == [
        "get_executor",
        "executor_close",
        "get_terminal",
        "terminal_shutdown",
        "close_dependencies",
    ]
