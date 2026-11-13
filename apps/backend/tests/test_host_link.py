"""宿主工具链路：认证、请求 / 应答配对、取消与时限、连接顶替与断开，以及错误到工具观察的映射。

``TestClient.websocket_connect`` 扮演桌面宿主；工具侧经 ``call_host`` / ``HostLink.submit`` 在测试线程
里调用，与生产中工具线程的用法一致。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.app import app
from app.core.tool.schemas import ToolExecutionContext
from app.core.workflow.cancel.tool_call_cancellation_registry import (
    tool_call_cancellation_registry,
)
from app.service.depends import get_host_link
from app.tools.host.errors import CANCELLED, TIMEOUT, UNAVAILABLE, HostCallError
from app.tools.host.host_call import call_host, host_error_observation
from app.tools.host.host_link import CLOSE_REPLACED, HOST_TOKEN_ENV, HostLink

TOKEN = "test-host-token"


@pytest.fixture
def link(monkeypatch: pytest.MonkeyPatch) -> Iterator[HostLink]:
    monkeypatch.setenv(HOST_TOKEN_ENV, TOKEN)
    get_host_link.cache_clear()
    yield get_host_link()
    get_host_link.cache_clear()


@pytest.fixture
def client() -> TestClient:
    # 不进入 lifespan：链路端点不依赖存储与运行时装配。
    return TestClient(app)


def _hello(token: str = TOKEN) -> dict[str, object]:
    return {"type": "hello", "token": token, "host": {"platform": "test", "pid": 1}}


def _wait_connected(link: HostLink, connected: bool = True) -> None:
    deadline = time.monotonic() + 2
    while (link._connection is not None) is not connected:
        assert time.monotonic() < deadline, "host link connection state did not settle"
        time.sleep(0.01)


def _context(tool_call_id: str = "call-1") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=7,
        workspace_id=1,
        workspace_root=Path("."),
        step_id="step",
        run_id=1,
        tool_call_id=tool_call_id,
    )


def test_wrong_token_is_rejected(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello("wrong"))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4003
    assert link._connection is None


def test_missing_backend_token_rejects_every_host(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV, raising=False)
    get_host_link.cache_clear()
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello(""))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4003
    get_host_link.cache_clear()


def test_first_frame_must_be_hello(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json({"id": 1, "result": None})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 1008


def test_requests_pair_with_out_of_order_replies(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello())
        _wait_connected(link)
        first = link.submit("demo.echo", {"n": 1}, {"session_id": 7})
        second = link.submit("demo.echo", {"n": 2}, {})
        requests = [ws.receive_json(), ws.receive_json()]
        by_n = {request["params"]["n"]: request for request in requests}
        assert by_n[1]["method"] == "demo.echo"
        assert by_n[1]["meta"] == {"session_id": 7}
        ws.send_json({"id": by_n[2]["id"], "result": "two"})
        ws.send_json({"id": by_n[1]["id"], "result": "one"})
        assert first.result(timeout=2) == "one"
        assert second.result(timeout=2) == "two"


def test_error_reply_surfaces_code_and_message(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello())
        _wait_connected(link)
        future = link.submit("demo.fail", {}, {})
        request = ws.receive_json()
        ws.send_json({"id": request["id"], "error": {"code": "TAB_NOT_FOUND", "message": "gone"}})
        with pytest.raises(HostCallError) as exc:
            future.result(timeout=2)
    assert (exc.value.code, exc.value.message) == ("TAB_NOT_FOUND", "gone")


def test_call_host_without_connection_is_unavailable(link: HostLink) -> None:
    with pytest.raises(HostCallError) as exc:
        call_host("demo.echo", {}, execution_context=_context(), timeout_seconds=5)
    assert exc.value.code == UNAVAILABLE


def test_cancelled_tool_call_sends_cancel_to_host(link: HostLink, client: TestClient) -> None:
    context = _context("call-cancel")
    with client.websocket_connect("/host/link") as ws, ThreadPoolExecutor(1) as pool:
        ws.send_json(_hello())
        _wait_connected(link)
        pending = pool.submit(
            call_host, "demo.slow", {}, execution_context=context, timeout_seconds=10
        )
        request = ws.receive_json()
        tool_call_cancellation_registry.mark_cancelled(context.session_id, context.tool_call_id)
        try:
            with pytest.raises(HostCallError) as exc:
                pending.result(timeout=2)
            cancel = ws.receive_json()
        finally:
            tool_call_cancellation_registry.clear(context.session_id, context.tool_call_id)
    assert exc.value.code == CANCELLED
    assert cancel == {"method": "$/cancel", "params": {"id": request["id"]}}


def test_timeout_abandons_request_and_notifies_host(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello())
        _wait_connected(link)
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(
                call_host, "demo.slow", {}, execution_context=None, timeout_seconds=0.3
            )
            request = ws.receive_json()
            with pytest.raises(HostCallError) as exc:
                pending.result(timeout=2)
        assert exc.value.code == TIMEOUT
        assert ws.receive_json() == {"method": "$/cancel", "params": {"id": request["id"]}}
        # 迟到的应答被丢弃，连接继续可用。
        ws.send_json({"id": request["id"], "result": "late"})
        future = link.submit("demo.echo", {}, {})
        follow_up = ws.receive_json()
        ws.send_json({"id": follow_up["id"], "result": "ok"})
        assert future.result(timeout=2) == "ok"


def test_newer_connection_replaces_older(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as old:
        old.send_json(_hello())
        _wait_connected(link)
        stale = link.submit("demo.slow", {}, {})
        old.receive_json()
        with client.websocket_connect("/host/link") as new:
            new.send_json(_hello())
            with pytest.raises(HostCallError) as exc:
                stale.result(timeout=2)
            assert exc.value.code == UNAVAILABLE
            with pytest.raises(WebSocketDisconnect) as closed:
                old.receive_json()
            assert closed.value.code == CLOSE_REPLACED
            future = link.submit("demo.echo", {}, {})
            request = new.receive_json()
            new.send_json({"id": request["id"], "result": "new"})
            assert future.result(timeout=2) == "new"


def test_disconnect_fails_pending_requests(link: HostLink, client: TestClient) -> None:
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello())
        _wait_connected(link)
        pending = link.submit("demo.slow", {}, {})
        ws.receive_json()
    with pytest.raises(HostCallError) as exc:
        pending.result(timeout=2)
    assert exc.value.code == UNAVAILABLE
    _wait_connected(link, connected=False)


def test_each_new_connection_is_announced_before_replies_are_read(
    link: HostLink, client: TestClient
) -> None:
    connected: list[bool] = []
    # 回调时这条连接已是当前连接：监听方经链路发出的请求能落到它上面。
    link.set_on_connected(lambda: connected.append(link._connection is not None))
    try:
        for _ in range(2):
            with client.websocket_connect("/host/link") as ws:
                ws.send_json(_hello())
                _wait_connected(link)
            _wait_connected(link, connected=False)
    finally:
        link.set_on_connected(None)
    assert connected == [True, True]


def test_notify_sends_without_waiting(link: HostLink, client: TestClient) -> None:
    link.notify("demo.dropped", {"session_ids": [1]})  # 未连接：只记日志
    with client.websocket_connect("/host/link") as ws:
        ws.send_json(_hello())
        _wait_connected(link)
        link.notify("demo.dropped", {"session_ids": [1, 2]})
        assert ws.receive_json() == {"method": "demo.dropped", "params": {"session_ids": [1, 2]}}


@pytest.mark.parametrize(
    ("code", "status", "hint"),
    [
        (CANCELLED, "cancelled", None),
        (UNAVAILABLE, "error", "宿主不可用"),
        (TIMEOUT, "error", "宿主超时"),
        ("INTERNAL", "error", "执行失败"),
    ],
)
def test_host_error_observation(code: str, status: str, hint: str | None) -> None:
    observation = host_error_observation("demo_tool", HostCallError(code, "detail"), "call-1")
    assert observation.status == status
    assert observation.tool_call_id == "call-1"
    if hint is not None:
        assert observation.display_data == {"status_hint": hint}
