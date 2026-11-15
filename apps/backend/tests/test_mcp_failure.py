"""MCP 失败归类：用户与模型看到的原因只在一处产生，且说得出下一步该改哪里。"""

from __future__ import annotations

import httpx
import pytest

from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.variable_references import MissingVariablesError
from app.tools.mcp.failure import MCPFailure, MCPFailureKind
from app.tools.mcp.oauth.callback_listener import OAuthCallbackError, OAuthCallbackTimeout
from app.tools.mcp.oauth.provider import MCPAuthorizationRequired

_REMOTE = ScopedMCPServer(
    ExtensionScope.system(),
    MCPServerConfig(
        name="github",
        transport="streamable_http",
        url="https://api.githubcopilot.com/mcp/",
        headers={"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"},
    ),
)
_LOCAL = ScopedMCPServer(
    ExtensionScope.system(), MCPServerConfig(name="everything", command="npx", args=["-y", "x"])
)


def _http_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.githubcopilot.com/mcp/")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


# 潜在缺陷：token 填错时用户只看到 HTTPStatusError 加一个 MDN 链接，不知道该改哪个变量。
def test_rejected_credentials_name_the_header_variable_even_inside_an_exception_group() -> None:
    failure = MCPFailure.from_exception(ExceptionGroup("tg", [_http_error(401)]), _REMOTE)

    assert failure.kind == MCPFailureKind.CREDENTIALS_REJECTED
    assert failure.for_user() == "服务拒绝了认证信息（401），请检查变量 GITHUB_PERSONAL_ACCESS_TOKEN 的值"
    assert "retrying will not help" in failure.for_model()


@pytest.mark.parametrize(
    ("exc", "server", "kind", "user_text"),
    [
        (MissingVariablesError(["A", "B"]), _REMOTE, MCPFailureKind.MISSING_VARIABLES, "A、B"),
        (MCPAuthorizationRequired("github"), _REMOTE, MCPFailureKind.AUTHORIZATION_REQUIRED, "需要登录授权"),
        (OAuthCallbackTimeout(), _REMOTE, MCPFailureKind.AUTHORIZATION_TIMEOUT, "等待浏览器授权超时"),
        (OAuthCallbackError("access_denied"), _REMOTE, MCPFailureKind.AUTHORIZATION_DENIED, "access_denied"),
        (_http_error(404), _REMOTE, MCPFailureKind.NOT_FOUND, "404"),
        (FileNotFoundError(2, "No such file"), _LOCAL, MCPFailureKind.COMMAND_NOT_FOUND, "找不到启动命令 npx"),
        (httpx.ConnectError("refused"), _REMOTE, MCPFailureKind.UNREACHABLE, "连不上服务"),
        (TimeoutError(), _LOCAL, MCPFailureKind.TIMEOUT, "首次启动可能在下载依赖"),
        (TimeoutError(), _REMOTE, MCPFailureKind.TIMEOUT, "连接超时"),
        (RuntimeError("weird"), _REMOTE, MCPFailureKind.OTHER, "RuntimeError: weird"),
    ],
)
def test_failures_are_classified_by_what_the_user_should_do_next(exc, server, kind, user_text) -> None:
    failure = MCPFailure.from_exception(exc, server)

    assert failure.kind == kind
    assert user_text in failure.for_user()
    assert server.name in failure.for_model()


def test_authorization_timeout_is_not_reported_as_a_connection_timeout() -> None:
    """授权等待超时也是 TimeoutError 子类，必须先于普通超时认出来。"""

    assert MCPFailure.from_exception(OAuthCallbackTimeout(), _LOCAL).kind == MCPFailureKind.AUTHORIZATION_TIMEOUT
