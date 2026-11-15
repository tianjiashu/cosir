"""一次 OAuth 授权的回调接收：在 ``127.0.0.1`` 上临时监听，收到浏览器带回的授权码即结束。

为什么每次授权单开一个监听、而不是挂在后端路由上：后端端口每次启动都变，长期开着一个不需认证的
回调入口也没有必要；单次监听随授权结束关闭。端口默认由系统分配（动态注册的客户端按本次地址注册），
服务要求预先登记回调地址时用配置里固定的 ``callback_port``。

只认 ``GET /callback``；``state`` 由 SDK 校验，这里只把参数交回去。
"""

from __future__ import annotations

import asyncio
from typing import Self
from urllib.parse import parse_qs, urlparse

from app.config.logging.logger import log

CALLBACK_PATH = "/callback"
_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>cosir</title>"
    "<body style='font-family:system-ui;padding:3rem;text-align:center'>"
    "<h2>{title}</h2><p>{message}</p></body>"
)


class OAuthCallbackError(RuntimeError):
    """授权服务把错误带回了回调地址（如用户拒绝授权）。"""


class OAuthCallbackTimeout(TimeoutError):
    """用户没在时限内完成浏览器里的授权。与连接超时分开，失败归类时据此说成「授权超时」。"""


class OAuthCallbackListener:
    """``async with`` 内监听回调；:meth:`wait` 返回 ``(code, state)``。"""

    def __init__(self, port: int | None) -> None:
        self._requested_port = port or 0
        self._server: asyncio.Server | None = None
        self._result: asyncio.Future[tuple[str, str | None]] | None = None

    @property
    def redirect_uri(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}{CALLBACK_PATH}"

    async def __aenter__(self) -> Self:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self._requested_port)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def wait(self) -> tuple[str, str | None]:
        assert self._result is not None
        return await self._result

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split(" ")
            target = urlparse(parts[1]) if len(parts) >= 2 else None
            if target is None or parts[0] != "GET" or target.path != CALLBACK_PATH:
                self._respond(writer, 404, "未找到", "这不是 cosir 的授权回调地址。")
                return
            query = {key: values[0] for key, values in parse_qs(target.query).items()}
            assert self._result is not None
            if "error" in query:
                self._respond(writer, 400, "授权未完成", "可以关闭这个页面，回到 cosir 重试。")
                if not self._result.done():
                    self._result.set_exception(
                        OAuthCallbackError(query.get("error_description") or query["error"])
                    )
                return
            if "code" not in query:
                self._respond(writer, 400, "授权未完成", "回调里缺少授权码。")
                return
            self._respond(writer, 200, "授权完成", "可以关闭这个页面，回到 cosir。")
            if not self._result.done():
                self._result.set_result((query["code"], query.get("state")))
        except (OSError, UnicodeError) as exc:
            log.warning(
                "mcp_oauth_callback_failed",
                extra={"msg": "OAuth 回调请求处理失败", "data": {"error_type": type(exc).__name__}},
            )
        finally:
            writer.close()

    @staticmethod
    def _respond(writer: asyncio.StreamWriter, status: int, title: str, message: str) -> None:
        body = _PAGE.format(title=title, message=message).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}[status]
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("latin-1") + body
        )
