"""SDK ``TokenStorage`` 协议在一个服务上的实现：读写本机记录里的授权（``mcp/machine_state``）。

``configured_client`` 是由配置里手填的 ``client_id`` 现算的客户端信息：有它时始终交给 SDK、不落盘，SDK 也就
不会去动态注册。``redirect_uri`` 是这次交互授权的回调地址；动态注册的客户端绑定了注册时的回调地址，端口
变了就不能再用，交互授权时丢弃让 SDK 重新注册（不交互时为 ``None``，刷新令牌用不到回调地址）。

不交互（连接测试、Agent 调用）且还没有注册过客户端时，交给 SDK 一个占位客户端：SDK 有了客户端信息就跳过
动态注册，直接走到「打开浏览器」那一步被拒（``provider._refuse_interaction``）。注册只在用户真要登录时才有
意义；否则批量测试、保存后自动测试每跑一次，都会在服务方白白注册一个新客户端。
"""

from __future__ import annotations

import time

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.config.logging.logger import log
from app.service.configuration.mcp.configuration import ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState, OAuthRecord

# 不交互时的占位客户端：只为让 SDK 跳过动态注册，拿着它的请求在打开浏览器之前就被拒绝，从不发出。
_UNREGISTERED_CLIENT = OAuthClientInformationFull(
    client_id="cosir-unregistered", redirect_uris=["http://127.0.0.1/callback"]
)


class ServerTokenStorage:
    def __init__(
        self,
        server: ScopedMCPServer,
        server_url: str,
        *,
        configured_client: OAuthClientInformationFull | None,
        redirect_uri: str | None,
    ) -> None:
        self._server = server
        self._server_url = server_url
        self._configured_client = configured_client
        self._redirect_uri = redirect_uri

    def stored_expiry(self) -> float | None:
        record = self._record()
        return record.expires_at if record is not None else None

    async def get_tokens(self) -> OAuthToken | None:
        record = self._record()
        if record is None or record.tokens is None:
            return None
        return OAuthToken.model_validate(record.tokens)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        expires_at = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        MCPMachineState.update_oauth(
            self._server,
            self._server_url,
            tokens=tokens.model_dump(mode="json"),
            expires_at=expires_at,
            needs_authorization=False,
        )
        log.info(
            "mcp_oauth_tokens_saved",
            extra={
                "msg": "MCP 服务的 OAuth 令牌已保存",
                "data": {"scope": self._server.scope.key, "server_name": self._server.name},
            },
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if self._configured_client is not None:
            return self._configured_client
        record = self._record()
        if record is None or record.client_info is None:
            return _UNREGISTERED_CLIENT if self._redirect_uri is None else None
        client = OAuthClientInformationFull.model_validate(record.client_info)
        registered = [str(uri) for uri in client.redirect_uris or []]
        if self._redirect_uri is not None and self._redirect_uri not in registered:
            return None
        return client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if self._configured_client is not None:
            return
        MCPMachineState.update_oauth(
            self._server, self._server_url, client_info=client_info.model_dump(mode="json")
        )

    def mark_needs_authorization(self) -> None:
        MCPMachineState.update_oauth(self._server, self._server_url, needs_authorization=True)

    def _record(self) -> OAuthRecord | None:
        return MCPMachineState.read().oauth(self._server, self._server_url)
