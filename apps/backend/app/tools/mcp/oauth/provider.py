"""给远程 MCP 服务挂上 OAuth：SDK 的 ``OAuthClientProvider`` 加上 cosir 的两条规则。

规则一：**只有用户点「测试连接」时才会打开浏览器授权**（``AuthorizationInteraction``）。Agent 调用与
连接池建连不交互：需要授权时记下「需重新授权」并抛 :class:`MCPAuthorizationRequired`，由工具把这件事
告诉模型、由配置页提示用户。运行中途弹出登录页会打断用户，也不知道是谁要授权。

规则二：请求头里已经带了 ``Authorization``（静态 token，常写成 ``Bearer ${TOKEN}``）的服务不挂 OAuth：
那是用户选定的认证方式，401 只说明 token 不对，不该转去走授权流程。

发现授权服务器、动态注册、PKCE、换取与刷新令牌都由 SDK 完成；令牌与注册信息的存放见
``service/configuration/mcp/machine_state``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

from app.config.logging.logger import log
from app.service.configuration.mcp.configuration import (
    MCPOAuthConfig,
    MCPServerConfig,
    ScopedMCPServer,
)
from app.tools.mcp.oauth.token_storage import ServerTokenStorage

# 用户在浏览器里完成授权的时限。
AUTHORIZATION_TIMEOUT_SECONDS = 300.0
# 不交互时 SDK 仍要求客户端元数据带回调地址；它只在注册与授权时用到，而这两步不交互时不会发生。
_UNUSED_REDIRECT_URI = "http://127.0.0.1/callback"


class MCPAuthorizationRequired(RuntimeError):
    """服务需要用户授权（首次授权，或令牌失效且刷新不了）；只能由用户在配置页的连接测试里完成。"""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        super().__init__(f"MCP server {server_name} needs authorization")


@dataclass(frozen=True)
class AuthorizationInteraction:
    """一次交互授权：回调地址、拿到授权地址后怎么交给用户、怎么等回调。"""

    redirect_uri: str
    on_redirect: Callable[[str], Awaitable[None]]
    wait_callback: Callable[[], Awaitable[tuple[str, str | None]]]


class _StoredExpiryProvider(OAuthClientProvider):
    """从存储加载令牌时一并恢复过期时间。

    SDK 加载存储的令牌时不恢复过期时间，过期的令牌会被当作有效发出去，收到 401 再走完整授权；在不交互
    的场景里这等于「重启后必须重新授权」，而本可以用刷新令牌静默续期。
    """

    def __init__(self, *, storage: ServerTokenStorage, **kwargs: object) -> None:
        super().__init__(storage=storage, **kwargs)  # type: ignore[arg-type]
        self._cosir_storage = storage

    async def _initialize(self) -> None:
        await super()._initialize()
        self.context.token_expiry_time = self._cosir_storage.stored_expiry()


def uses_oauth(config: MCPServerConfig) -> bool:
    """远程服务且请求头没有自带 ``Authorization`` 时走 OAuth（服务不要求认证时 OAuth 也不会触发）。"""

    return config.transport != "stdio" and not any(
        key.lower() == "authorization" for key in config.headers
    )


def oauth_auth(
    server: ScopedMCPServer,
    config: MCPServerConfig,
    interaction: AuthorizationInteraction | None,
) -> httpx.Auth | None:
    """为变量已展开的建连配置构造 httpx 认证；stdio 服务与自带 ``Authorization`` 头的服务返回 ``None``。"""

    if not uses_oauth(config):
        return None
    oauth = config.oauth or MCPOAuthConfig()
    redirect_uri = interaction.redirect_uri if interaction is not None else _UNUSED_REDIRECT_URI
    storage = ServerTokenStorage(
        server,
        config.url,
        configured_client=_configured_client(oauth, redirect_uri),
        redirect_uri=interaction.redirect_uri if interaction is not None else None,
    )
    if interaction is None:
        redirect_handler = _refuse_interaction(server, storage)
        callback_handler = _never_called
    else:
        redirect_handler = _with_configured_scope(interaction.on_redirect, oauth.scope)
        callback_handler = interaction.wait_callback
    return _StoredExpiryProvider(
        server_url=config.url,
        client_metadata=OAuthClientMetadata(
            client_name="cosir",
            redirect_uris=[redirect_uri],
            token_endpoint_auth_method="none",  # noqa: S106 公共客户端的认证方式名，不是凭据
            scope=oauth.scope or None,
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=AUTHORIZATION_TIMEOUT_SECONDS,
    )


def _configured_client(oauth: MCPOAuthConfig, redirect_uri: str) -> OAuthClientInformationFull | None:
    """手填 ``client_id`` 时由配置现算客户端信息，SDK 拿到它就不会动态注册。"""

    if not oauth.client_id:
        return None
    return OAuthClientInformationFull(
        client_id=oauth.client_id,
        client_secret=oauth.client_secret or None,
        redirect_uris=[redirect_uri],
        token_endpoint_auth_method="client_secret_post" if oauth.client_secret else "none",
        scope=oauth.scope or None,
    )


def _refuse_interaction(
    server: ScopedMCPServer, storage: ServerTokenStorage
) -> Callable[[str], Awaitable[None]]:
    async def refuse(_authorization_url: str) -> None:
        storage.mark_needs_authorization()
        log.warning(
            "mcp_oauth_authorization_required",
            extra={
                "msg": "MCP 服务需要用户重新授权，Agent 调用不打开浏览器",
                "data": {"scope": server.scope.key, "server_name": server.name},
            },
        )
        raise MCPAuthorizationRequired(server.name)

    return refuse


async def _never_called() -> tuple[str, str | None]:
    raise AssertionError("不交互的授权在打开浏览器之前就已拒绝")


def _with_configured_scope(
    on_redirect: Callable[[str], Awaitable[None]], scope: str
) -> Callable[[str], Awaitable[None]]:
    """服务没有声明 scope 时用配置里写的（服务声明了就以服务为准，与 Codex 一致）。

    SDK 在授权前按服务声明重算 scope、没有声明时去掉 scope 参数，配置值进不了授权地址，只能在这里补上。
    """

    async def redirect(authorization_url: str) -> None:
        if scope:
            parsed = urlparse(authorization_url)
            query = parse_qsl(parsed.query)
            if not any(key == "scope" for key, _ in query):
                authorization_url = urlunparse(
                    parsed._replace(query=urlencode([*query, ("scope", scope)]))
                )
        await on_redirect(authorization_url)

    return redirect
