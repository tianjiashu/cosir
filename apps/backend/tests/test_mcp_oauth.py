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
    # 像 OAuth 2.1 要求公共客户端那样轮换 refresh token：每次刷新都作废用掉的那一个。
    valid_refresh_tokens: set[str] = field(default_factory=set)
    issued: int = 0
    expires_in: int = 3600
    # 置位后 MCP 端点对请求一律回 500（模拟长连接中途服务端出错）。
    failing: bool = False

    def issue(self) -> dict[str, object]:
        self.issued += 1
        access = f"access-{self.issued}"
        refresh = f"refresh-{self.issued}"
        self.valid_tokens.add(access)
        self.valid_refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
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
                [refresh] = form["refresh_token"]
                if refresh not in state.valid_refresh_tokens:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                state.valid_refresh_tokens.discard(refresh)
                state.refreshes += 1
            return JSONResponse(state.issue())
        if path.startswith("/mcp"):
            if state.failing and request.method == "POST":
                return PlainTextResponse("boom", status_code=500)
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
    MCPMachineState.record_pass(server, [])
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


def _page_status(name: str = "remote") -> str:
    """配置页上这个服务的主状态（与 ``GET`` 服务列表同一条推导）。"""

    [entry] = [item for item in MCPConfigurationService().visible() if item.server.name == name]
    return entry.status


# B1 潜在缺陷：授权记录按服务名只存一条、地址一变就整条换掉，测一份改了地址的草稿（不保存）就把
# 已保存服务的令牌抹掉；配置页查不到旧地址的记录，只看「测试通过」，还显示「可用」。
def test_testing_a_draft_with_another_url_keeps_the_saved_servers_sign_in() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            saved = _server(base)
            MCPConfigurationService().update([saved.config])
            assert (await _sign_in(saved)).success is True
            assert _page_status() == "available"

            # 编辑框里把地址改成另一个（同一服务的不同路径）去测试，不保存。
            draft = saved.config.model_copy(update={"url": f"{base}/mcp?draft=1"})
            probe = await MCPConfigurationService().test(draft)
            assert probe.success is False

            record = MCPMachineState.read().oauth(saved, f"{base}/mcp")
            assert record is not None and record.tokens is not None, "已保存服务的令牌被草稿测试抹掉了"
            assert json.loads(await _call_through_pool(saved)) == {"echo": "hi"}
            assert _page_status() == "available"

    asyncio.run(scenario())


# B2 潜在缺陷：「需要重新登录」只在拿到新令牌时清掉；改用静态 token 后测试通过，页面仍卡在「需要登录授权」。
def test_passing_test_clears_a_stale_sign_in_required_mark() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)
            MCPConfigurationService().update([server.config])
            with pytest.raises(BaseException) as error:
                await _call_through_pool(server)  # Agent 调用发现没有令牌，打上「需要重新登录」
            assert MCPFailure.from_exception(error.value, server).kind == MCPFailureKind.AUTHORIZATION_REQUIRED
            assert _page_status() == "authorization_required"

            # 用户改为在请求头里写静态 token（地址不变），保存并测试，测试通过。
            auth.valid_tokens.add("static-token")
            fixed = server.config.model_copy(update={"headers": {"Authorization": "Bearer static-token"}})
            MCPConfigurationService().update([fixed])
            result = await MCPConfigurationService().test(fixed)
            assert result.success is True

            assert _page_status() == "available", "测试已通过，页面仍显示需要登录授权"

    asyncio.run(scenario())


# 潜在缺陷：为了不让草稿影响已保存配置而不让草稿落盘，会让「新建服务、先登录、再保存」丢掉令牌。
def test_signing_in_before_saving_keeps_the_sign_in_after_saving() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            draft = _server(base)
            assert (await _sign_in(draft)).success is True

            MCPConfigurationService().update([draft.config])

            assert _page_status() == "available"
            assert json.loads(await _call_through_pool(draft)) == {"echo": "hi"}
            assert auth.issued == 1

    asyncio.run(scenario())


def test_saving_keeps_only_the_current_url_sign_in() -> None:
    """授权按地址分开存：保存后只留当前地址的那份，测过的草稿地址随之裁掉。"""

    saved = _server("http://127.0.0.1:1")
    MCPMachineState.update_oauth(saved, "http://127.0.0.1:1/mcp", tokens={"access_token": "kept"})
    MCPMachineState.update_oauth(saved, "http://127.0.0.1:1/other", needs_authorization=True)

    MCPConfigurationService().update([saved.config])

    assert set(MCPMachineState.read().server(saved).oauth) == {"http://127.0.0.1:1/mcp"}


# B3 潜在缺陷：SDK 只在初始化时读一次存储；配置页测试刷新令牌、轮换掉 refresh token 后，连接池长连接手里的
# 旧 refresh token 作废，下次刷新失败就被判成「需要登录」。
def test_a_long_lived_connection_refreshes_with_the_latest_stored_token() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            auth.expires_in = 1
            server = _server(base)
            assert (await _sign_in(server)).success is True
            pool = MCPConnectionPool()
            try:

                async def call() -> str:
                    future = pool.submit(pool.call_tool(server, "echo", {"value": "hi"}))
                    return MCPCallResult.from_sdk(await asyncio.wrap_future(future)).text

                assert json.loads(await call()) == {"echo": "hi"}  # 长连接拿着第一份令牌
                await asyncio.sleep(1.2)
                # 配置页测试用另一个 provider 刷新，服务端轮换掉第一份 refresh token。
                assert (await MCPConfigurationService().test(server.config)).success is True
                assert auth.refreshes == 1
                await asyncio.sleep(1.2)

                assert json.loads(await call()) == {"echo": "hi"}
                assert auth.refreshes == 2
                assert MCPMachineState.read().oauth(server, f"{base}/mcp").needs_authorization is False
            finally:
                await asyncio.to_thread(pool.close)

    asyncio.run(scenario())


async def _call_with_deadline(pool: MCPConnectionPool, server: ScopedMCPServer) -> str:
    """经长连接调用一次；10 秒兜底只为让挂住的调用变成失败的断言，而不是卡住整个测试。"""

    future = pool.submit(pool.call_tool(server, "echo", {"value": "hi"}))
    result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=10)
    return MCPCallResult.from_sdk(result).text


# 潜在缺陷：长连接建好之后服务端撤销了令牌（refresh 也失效），授权失败发生在传输层的后台任务里，没有传回
# 正在等响应的调用，调用一直挂到外层 90 秒时限，最后被说成「调用超时、服务可能已执行」。
def test_revoked_sign_in_on_a_live_connection_fails_the_call_promptly() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)
            assert (await _sign_in(server)).success is True
            pool = MCPConnectionPool()
            try:
                assert json.loads(await _call_with_deadline(pool, server)) == {"echo": "hi"}
                auth.valid_tokens.clear()
                auth.valid_refresh_tokens.clear()

                with pytest.raises(BaseException) as error:
                    await _call_with_deadline(pool, server)

                assert MCPFailure.from_exception(error.value, server).kind == MCPFailureKind.AUTHORIZATION_REQUIRED
                assert MCPMachineState.read().oauth(server, f"{base}/mcp").needs_authorization is True
            finally:
                await asyncio.to_thread(pool.close)

    asyncio.run(scenario())


# 同一根因的另一种表现：长连接中途服务端回 500，调用同样挂住而不是报连接失败。
def test_server_error_on_a_live_connection_fails_the_call_promptly() -> None:
    async def scenario() -> None:
        async with _protected_server() as (auth, base):
            server = _server(base)
            assert (await _sign_in(server)).success is True
            pool = MCPConnectionPool()
            try:
                assert json.loads(await _call_with_deadline(pool, server)) == {"echo": "hi"}
                auth.failing = True

                with pytest.raises(BaseException) as error:
                    await _call_with_deadline(pool, server)

                assert not isinstance(error.value, asyncio.TimeoutError), "调用挂住了，没有报错"
            finally:
                await asyncio.to_thread(pool.close)

    asyncio.run(scenario())
