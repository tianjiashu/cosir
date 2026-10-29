"""未捕获异常的 HTTP 响应契约：结构化 500 且带 CORS 头。

缺 CORS 头时 WebView 只看到网络失败，前端会把任何后端 bug 误报为「无法连接本机后端」。
"""

from fastapi.testclient import TestClient

from app.app import app


def test_unhandled_exception_returns_structured_500_with_cors_headers() -> None:
    @app.get("/__test__/boom")
    async def _boom() -> None:
        raise RuntimeError("raw internal detail")

    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/__test__/boom", headers={"Origin": "http://tauri.localhost"}
        )
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert "raw internal detail" not in error["message"]
    assert response.headers["x-trace-id"] in error["message"]
