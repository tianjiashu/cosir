from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.core.observability.langfuse_runtime import (
    LangfuseConfig,
    LangfuseRuntimeManager,
    TraceMetadata,
)
from app.core.observability.tool_trace_recorder import _NullToolTraceRecorder
from app.core.tools.schemas import ToolCall, ToolObservation


@dataclass
class _FakeContext:
    value: Any = None
    entered: bool = False
    exited: bool = False

    def __enter__(self) -> Any:
        self.entered = True
        return self.value

    def __exit__(self, *_exc_info: object) -> None:
        self.exited = True


class _FakeSpan:
    trace_id = "trace-1"

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class _FakeClient:
    def __init__(self, config: LangfuseConfig) -> None:
        self.config = config
        self.flush_count = 0
        self.shutdown_count = 0
        self.root_contexts: list[_FakeContext] = []

    def start_as_current_observation(self, **_kwargs: object) -> _FakeContext:
        context = _FakeContext(_FakeSpan())
        self.root_contexts.append(context)
        return context

    def flush(self) -> None:
        self.flush_count += 1

    def shutdown(self) -> None:
        self.shutdown_count += 1


class _FakeSdk:
    def __init__(self) -> None:
        self.clients: list[_FakeClient] = []
        self.reset_public_keys: list[str] = []
        self.callback_calls = 0
        self.propagate_contexts: list[_FakeContext] = []

    def is_available(self) -> bool:
        return True

    def build_client(self, config: LangfuseConfig) -> _FakeClient:
        client = _FakeClient(config)
        self.clients.append(client)
        return client

    def build_callback_handler(self, _public_key: str) -> object:
        self.callback_calls += 1
        return object()

    def propagate_attributes(self, **_kwargs: object):
        context = _FakeContext()
        self.propagate_contexts.append(context)
        return context

    def reset_resource(self, public_key: str) -> None:
        self.reset_public_keys.append(public_key)


def _config(*, enabled: bool = True, credential: str = "secret") -> LangfuseConfig:
    return LangfuseConfig(
        enabled=enabled,
        public_key="public",
        secret_key=credential,
        base_url="https://langfuse.example.com",
    )


def _metadata() -> TraceMetadata:
    return TraceMetadata(task_id=1, run_id=2, agent_id="main_agent")


@pytest.mark.asyncio
async def test_root_and_tool_trace_are_created_as_one_runtime_context() -> None:
    sdk = _FakeSdk()
    manager = LangfuseRuntimeManager(sdk=sdk)
    manager.reload(_config())

    async with manager.conversation_run_trace(_metadata()) as result:
        assert result.trace_id == "trace-1"
        assert not isinstance(result.tool_trace_recorder, _NullToolTraceRecorder)
        assert result.callbacks
        with result.tool_trace_recorder.span(
            ToolCall(tool_name="read_file", call_id="call-1"), "step-1"
        ) as tool_span:
            tool_span.record(
                ToolObservation(
                    tool_name="read_file",
                    status="success",
                    content="ok",
                    tool_call_id="call-1",
                )
            )

    client = sdk.clients[0]
    assert len(client.root_contexts) == 2
    assert client.root_contexts[0].entered is True
    assert client.root_contexts[0].exited is True
    assert client.root_contexts[1].entered is True
    assert client.root_contexts[1].exited is True
    assert sdk.propagate_contexts[0].entered is True
    assert sdk.propagate_contexts[0].exited is True


@pytest.mark.asyncio
async def test_reload_to_disabled_flushes_and_closes_existing_client() -> None:
    sdk = _FakeSdk()
    manager = LangfuseRuntimeManager(sdk=sdk)
    manager.reload(_config())

    async with manager.conversation_run_trace(_metadata()):
        pass

    manager.reload(_config(enabled=False))

    client = sdk.clients[0]
    assert client.flush_count == 1
    assert client.shutdown_count == 1
    assert sdk.reset_public_keys == ["public"]


@pytest.mark.asyncio
async def test_shutdown_flushes_client_even_after_langfuse_is_disabled() -> None:
    sdk = _FakeSdk()
    manager = LangfuseRuntimeManager(sdk=sdk)
    manager.reload(_config())

    async with manager.conversation_run_trace(_metadata()):
        pass

    manager.reload(_config(enabled=False))
    manager.shutdown()

    client = sdk.clients[0]
    assert client.flush_count == 1
    assert client.shutdown_count == 1


@pytest.mark.asyncio
async def test_shutdown_flushes_active_client_and_defers_close_until_run_finishes() -> None:
    sdk = _FakeSdk()
    manager = LangfuseRuntimeManager(sdk=sdk)
    manager.reload(_config())

    async with manager.conversation_run_trace(_metadata()):
        manager.reload(_config(enabled=False))
        manager.shutdown()
        client = sdk.clients[0]
        assert client.flush_count == 1
        assert client.shutdown_count == 0

    client = sdk.clients[0]
    assert client.shutdown_count == 1


@pytest.mark.asyncio
async def test_active_run_keeps_old_client_until_it_finishes() -> None:
    sdk = _FakeSdk()
    manager = LangfuseRuntimeManager(sdk=sdk)
    manager.reload(_config())

    async def acquire_next_trace() -> str | None:
        async with manager.conversation_run_trace(_metadata()) as result:
            return result.trace_id

    async with manager.conversation_run_trace(_metadata()) as result:
        assert result.trace_id == "trace-1"
        rotated_credential = "new-secret"
        manager.reload(_config(credential=rotated_credential))
        pending = asyncio.create_task(acquire_next_trace())
        await asyncio.sleep(0)
        assert not pending.done()

    assert await pending == "trace-1"

    assert len(sdk.clients) == 2
    assert sdk.clients[1].config.secret_key == rotated_credential
    assert sdk.clients[0].flush_count == 1
    assert sdk.clients[0].shutdown_count == 1


@pytest.mark.asyncio
async def test_tool_trace_initialization_failure_rolls_back_root_trace() -> None:
    sdk = _FakeSdk()

    def fail_tool_recorder(_client: object) -> object:
        raise RuntimeError("tool recorder unavailable")

    manager = LangfuseRuntimeManager(sdk=sdk, tool_recorder_factory=fail_tool_recorder)
    manager.reload(_config())

    async with manager.conversation_run_trace(_metadata()) as result:
        assert result.trace_id is None
        assert result.callbacks == []
        assert isinstance(result.tool_trace_recorder, _NullToolTraceRecorder)

    assert sdk.clients[0].root_contexts[0].exited is True
    assert sdk.propagate_contexts[0].exited is True
