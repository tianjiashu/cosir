import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from app.api.middleware.local_identity import LocalIdentityMiddleware


def test_local_identity_is_required_for_http_and_websocket(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_API_TOKEN", "isolated-test-capability")

    async def http(_request):
        return PlainTextResponse("ok")

    async def ws(socket):
        await socket.accept()
        await socket.send_text("ok")
        await socket.close()

    app = Starlette(
        routes=[Route("/health", http), Route("/control", http), WebSocketRoute("/ws", ws)],
        middleware=[Middleware(LocalIdentityMiddleware)],
    )
    with TestClient(app, headers={}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/control").status_code == 403
        assert client.get("/control", headers={"X-Cosir-Capability": "wrong"}).status_code == 403
        headers = {"X-Cosir-Capability": "isolated-test-capability"}
        assert client.get("/control", headers=headers).status_code == 200
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws"):
            pass
        with client.websocket_connect("/ws", headers=headers) as socket:
            assert socket.receive_text() == "ok"
