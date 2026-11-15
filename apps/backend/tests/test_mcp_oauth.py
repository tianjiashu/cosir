"""远程 MCP 服务的 OAuth：连接测试里交互授权，Agent 调用静默用令牌、过期刷新、失效时不打开浏览器。

本机起一个最小授权服务器（受保护资源元数据、授权服务器元数据、动态注册、授权、换令牌）挡在一个真实的
FastMCP 服务前面，SDK 走的是真实的发现、注册、PKCE 与刷新流程。
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.configuration_service import (
    MCPConfigurationService,
    MCPServerTestResult,
    PendingAuthorization,
)
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.connection_pool import MCPConnectionPool
from app.tools.mcp.failure import MCPFailure, MCPFailureKind


@dataclass
class _AuthServer:
    base: str = ""
    registrations: int = 0
    refreshes: int = 0
    authorize_queries: list[dict[str, str]] = field(default_factory=list)
    valid_tokens: set[str] = field(default_factory=set)
    issued: int = 0

    def issue(self) -> dict[str, object]:
        self.issued += 1
        access = f"access-{self.issued}"
        self.valid_tokens.add(access)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": f"refresh-{self.issued}",
        }


@asynccontextmanager
async def _protected_server() -> AsyncIterator[tuple[_AuthServer, str]]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    state = _AuthServer(base=base)
    mcp_server = FastMCP("oauth-server", host="127.0.0.1", port=listener.getsockname()[1])

    @mcp_server.tool()
    def echo(value: str) -> dict:
        return {"echo": value}

    app = mcp_server.streamable_http_app()

    async def authorization_server(request, call_next):
        path = request.url.path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})
        if path.startswith("/.well-known/oauth-authorization-server"):
            return JSONResponse(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "registration_endpoint": f"{base}/register",
                    "response_types_supported": ["code"],
                    "code_challenge_methods_supported": ["S256"],
                }
            )
        if path == "/register":
            state.registrations += 1
            body = json.loads(await request.body())
            return JSONResponse(
                {**body, "client_id": f"client-{state.registrations}"}, status_code=201
            )
        if path == "/authorize":
            query = dict(request.query_params)
            state.authorize_queries.append(query)
            target = f"{query['redirect_uri']}?{urlencode({'code': 'the-code', 'state': query['state']})}"
            return RedirectResponse(target, status_code=302)
        if path == "/token":
            form = parse_qs((await request.body()).decode())
            if form["grant_type"] == ["refresh_token"]:
                state.refreshes += 1
            return JSONResponse(state.issue())
        if path.startswith("/mcp"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token not in state.valid_tokens:
                return PlainTextResponse(
                    "unauthorized",
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
                        )
                    },
                )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=authorization_server)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield state, base
    finally:
        server.should_exit = True
        await task


def _server(base: str, **oauth: object) -> ScopedMCPServer:
    config = MCPServerConfig(
        name="remote",
        transport="streamable_http",
        url=f"{base}/mcp",
        **({"oauth": oauth} if oauth else {}),
    )
    return ScopedMCPServer(ExtensionScope.system(), config)


async def _open_in_browser(url: str) -> None:
    """替用户在浏览器里点「同意」：授权服务器把浏览器重定向回 cosir 的回调地址。"""

    async with httpx.AsyncClient(follow_redirects=True) as browser:
        page = await browser.get(url)
    assert "授权完成" in page.text


async def _sign_in(server: ScopedMCPServer) -> MCPServerTestResult:
    """配置页的「登录授权」：开始授权、替用户在浏览器里同意、取最终结果。"""

    service = MCPConfigurationService()
    pending = await service.authorize(server.config)
    assert isinstance(pending, PendingAuthorization)
    await _open_in_browser(pending.authorization_url)
    return await service.wait_authorization(pending.flow_id)


async def _call_through_pool(server: ScopedMCPServer) -> str:
    pool = MCPConnectionPool()
    try:
        result = await asyncio.wrap_future(pool.submit(pool.call_tool(server, "echo", {"value": "hi"})))
    finally:
        await asyncio.to_thread(pool.close)
    return MCPCallResult.from_sdk(result).text


def test_test_reports_sign_in_needed_then_sign_in_works_and_tokens_are_reused_and_refreshed() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)

            # 连接测试从不打开浏览器：只得出「需要登录授权」。
            probe = await MCPConfigurationService().test(server.config)
            assert (probe.success, probe.failure_kind) == (False, "authorization_required")
            assert auth.authorize_queries == []

            result = await _sign_in(server)

            assert (result.success, [tool["name"] for tool in result.tools]) == (True, ["echo"])
            assert MCPMachineState.read().passed(server)
            assert auth.registrations == 1
            # Agent 调用不交互，直接用存好的令牌。
            assert json.loads(await _call_through_pool(server)) == {"echo": "hi"}
            assert auth.issued == 1

            # 重启后令牌已过期：用刷新令牌静默续期，而不是要求重新授权。
            MCPMachineState.update_oauth(server, f"{base}/mcp", expires_at=time.time() - 10)
            assert json.loads(await _call_through_pool(server)) == {"echo": "hi"}
            assert auth.refreshes == 1

    asyncio.run(scenario())


# 潜在缺陷：Agent 运行到一半为了授权去开浏览器，或者授权失效时只报一句笼统的连接失败。
def test_agent_calls_never_open_the_browser_and_mark_reauthorization() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)

            with pytest.raises(BaseException) as error:
                await _call_through_pool(server)

            failure = MCPFailure.from_exception(error.value, server)
            assert failure.kind == MCPFailureKind.AUTHORIZATION_REQUIRED
            assert "Sign in" in failure.for_model()
            assert auth.authorize_queries == []
            record = MCPMachineState.read().oauth(server, f"{base}/mcp")
            assert record is not None and record.needs_authorization is True

            await _sign_in(server)
            assert MCPMachineState.read().oauth(server, f"{base}/mcp").needs_authorization is False

    asyncio.run(scenario())


def test_pre_registered_client_skips_registration_and_uses_configured_scope(monkeypatch) -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            monkeypatch.setenv("REMOTE_CLIENT_SECRET", "s3cret")
            server = _server(
                base,
                client_id="registered-app",
                client_secret="${REMOTE_CLIENT_SECRET}",  # noqa: S106 变量引用，不是凭据
                scope="repo read:org",
            )

            result = await _sign_in(server)

            assert result.success is True
            assert auth.registrations == 0
            [query] = auth.authorize_queries
            assert query["client_id"] == "registered-app"
            assert query["scope"] == "repo read:org"
            assert urlparse(query["redirect_uri"]).hostname == "127.0.0.1"

    asyncio.run(scenario())


def test_static_authorization_header_is_not_replaced_by_oauth() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            config = MCPServerConfig(
                name="remote",
                transport="streamable_http",
                url=f"{base}/mcp",
                headers={"Authorization": "Bearer wrong"},
            )

            result = await MCPConfigurationService().test(config)

            assert (result.success, result.failure_kind) == (False, "credentials_rejected")
            assert auth.authorize_queries == [] and auth.registrations == 0

    asyncio.run(scenario())


def test_saving_drops_test_records_and_credentials_of_removed_servers_together() -> None:
    """测试记录与授权是同一份本机记录：删掉服务时一处清理，不会只清掉一半。"""

    server = _server("http://127.0.0.1:1")
    MCPMachineState.record_pass(server)
    MCPMachineState.update_oauth(server, "http://127.0.0.1:1/mcp", tokens={"access_token": "x"})

    MCPConfigurationService().update([])

    assert MCPMachineState.read().scopes == {}


# 潜在缺陷：用户放弃浏览器授权后，后端还开着回调监听等满五分钟，期间再授权会撞上旧流程。
def test_cancelled_sign_in_stops_waiting_and_a_new_sign_in_works() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)
            service = MCPConfigurationService()
            abandoned = await service.authorize(server.config)
            assert isinstance(abandoned, PendingAuthorization)
            waiting = asyncio.create_task(MCPConfigurationService.wait_authorization(abandoned.flow_id))
            await asyncio.sleep(0)

            MCPConfigurationService.cancel_authorization(abandoned.flow_id)

            # 正在等的请求得到「这次授权已不存在」，而不是裸的取消异常。
            with pytest.raises(KeyError):
                await waiting
            assert (await _sign_in(server)).success is True

    asyncio.run(scenario())
