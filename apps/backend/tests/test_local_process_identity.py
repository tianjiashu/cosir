"""本机控制面身份边界：HTTP/WS 统一校验、公开路径、fail closed 与拒绝日志。"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.middleware import local_identity
from app.api.middleware.local_identity import (
    LocalIdentityMiddleware,
    warn_if_capability_missing,
)

_TOKEN = "isolated-test-capability"  # noqa: S105 - 测试占位凭据
_HEADERS = {"X-Cosir-Capability": _TOKEN}


async def _http(_request):
    return PlainTextResponse("ok")


async def _ws(socket):
    await socket.accept()
    await socket.send_text("ok")
    await socket.close()


def _app() -> Starlette:
    """最小应用：公开探针、受控接口、受控 WebSocket 与宿主链路。"""

    return Starlette(
        routes=[
            Route("/health", _http),
            Route("/control", _http),
            WebSocketRoute("/ws", _ws),
            WebSocketRoute("/host/link", _ws),
        ],
        middleware=[Middleware(LocalIdentityMiddleware)],
    )


def test_local_identity_is_required_for_http_and_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 与 WebSocket 在同一处验证；错误或缺失凭据分别得到 403 与 4403 关闭。"""

    monkeypatch.setenv("CODING_AGENT_API_TOKEN", _TOKEN)

    with TestClient(_app(), headers={}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/control").status_code == 403
        assert client.get("/control", headers={"X-Cosir-Capability": "wrong"}).status_code == 403
        assert client.get("/control", headers=_HEADERS).status_code == 200
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
            pass
        with client.websocket_connect("/ws", headers=_HEADERS) as socket:
            assert socket.receive_text() == "ok"


def test_missing_token_rejects_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """未注入身份时必须 fail closed：任何凭据都不被接受，只有公开探针仍可用。"""

    monkeypatch.delenv("CODING_AGENT_API_TOKEN", raising=False)

    with TestClient(_app(), headers={}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/control").status_code == 403
        assert client.get("/control", headers=_HEADERS).status_code == 403
        assert client.get("/control", headers={"X-Cosir-Capability": ""}).status_code == 403
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws", headers=_HEADERS):
            pass


def test_host_link_keeps_its_own_first_frame_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主链路豁免本层校验：它由路由在自己的首帧校验 token，网页因此连上也会被拒。"""

    monkeypatch.setenv("CODING_AGENT_API_TOKEN", _TOKEN)

    with TestClient(_app(), headers={}) as client, client.websocket_connect("/host/link") as socket:
        assert socket.receive_text() == "ok"


def test_rejection_logs_reason_and_dedupes_within_window(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """被拒请求落一条 WARNING（含路径与凭据是否出现），同一窗口内同键只记一条。"""

    monkeypatch.setenv("CODING_AGENT_API_TOKEN", _TOKEN)
    monkeypatch.setattr(local_identity, "_last_reject_logged", {})

    with TestClient(_app(), headers={}) as client:
        assert client.get("/control").status_code == 403
        assert client.get("/control").status_code == 403
        rejected = [r for r in caplog.records if r.message == "local_identity_rejected"]
        assert len(rejected) == 1
        assert rejected[0].levelname == "WARNING"
        assert rejected[0].data == {
            "kind": "http",
            "method": "GET",
            "path": "/control",
            "capability_present": False,
            "token_configured": True,
            "trace_id": "",
        }

        # 换了「是否带凭据」这个维度就是另一个调用方形态，不受上一个键的限流影响。
        assert client.get("/control", headers={"X-Cosir-Capability": "wrong"}).status_code == 403
        rejected = [r for r in caplog.records if r.message == "local_identity_rejected"]
        assert len(rejected) == 2
        assert rejected[1].data["capability_present"] is True


def test_startup_warns_only_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """启动期只在身份缺失时提醒一次：否则表现为「界面全崩、后端日志干净」。"""

    monkeypatch.delenv("CODING_AGENT_API_TOKEN", raising=False)
    warn_if_capability_missing()
    warned = [r for r in caplog.records if r.message == "local_identity_token_missing"]
    assert len(warned) == 1
    assert warned[0].levelname == "WARNING"
    assert warned[0].data == {"env": "CODING_AGENT_API_TOKEN"}

    caplog.clear()
    monkeypatch.setenv("CODING_AGENT_API_TOKEN", _TOKEN)
    warn_if_capability_missing()
    assert caplog.records == []
