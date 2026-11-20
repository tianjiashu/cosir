"""MCP 连接与调用失败的归类：用户和模型看到的失败原因只在这里产生。

建连、授权、调用里冒出来的异常五花八门（SDK 的 task group 还会把它们包进 ``ExceptionGroup``），直接把
异常文本交出去，用户看到的是 ``HTTPStatusError … developer.mozilla.org``，不知道该改哪里。这里按「用户
下一步该做什么」归成少数几类，各给一句中文（配置页）和一句英文（模型）；原始异常照旧只进日志
（``client.describe_exception``）。

不负责调用层面的结局：调用超时（请求已放弃、服务可能已执行）、参数不合格、工具不存在由 ``mcp_tools``
各自说明，它们不是连接或授权的问题。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.service.configuration.mcp.configuration import ScopedMCPServer
from app.service.configuration.mcp.variable_references import (
    MissingVariablesError,
    find_references,
)
from app.tools.mcp.client import describe_exception
from app.tools.mcp.oauth.callback_listener import OAuthCallbackError, OAuthCallbackTimeout
from app.tools.mcp.oauth.provider import MCPAuthorizationRequired


class MCPFailureKind(StrEnum):
    MISSING_VARIABLES = "missing_variables"
    AUTHORIZATION_REQUIRED = "authorization_required"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_TIMEOUT = "authorization_timeout"
    CREDENTIALS_REJECTED = "credentials_rejected"
    NOT_FOUND = "not_found"
    UNREACHABLE = "unreachable"
    COMMAND_NOT_FOUND = "command_not_found"
    TIMEOUT = "timeout"
    OTHER = "other"


# 「下一步是登录授权」：首次需要授权、授权超时、授权被拒都一样，用户再点一次「登录授权」即可。
_SIGN_IN_KINDS = frozenset(
    {
        MCPFailureKind.AUTHORIZATION_REQUIRED,
        MCPFailureKind.AUTHORIZATION_TIMEOUT,
        MCPFailureKind.AUTHORIZATION_DENIED,
    }
)
# 失败多半是一时的（服务没起、网络抖动），或者缺的是凭据而不是定义本身有错：都不说明这份定义在本机用不了，
# 测试记录保留。认不出原因的（``OTHER``）按「失败即关闭」撤销。
_KEEPS_TEST_KINDS = _SIGN_IN_KINDS | {MCPFailureKind.UNREACHABLE, MCPFailureKind.TIMEOUT}

_STATUS_HINTS: dict[MCPFailureKind, str] = {
    MCPFailureKind.MISSING_VARIABLES: "缺少变量",
    MCPFailureKind.AUTHORIZATION_REQUIRED: "需要登录",
    MCPFailureKind.AUTHORIZATION_DENIED: "授权未完成",
    MCPFailureKind.AUTHORIZATION_TIMEOUT: "授权超时",
    MCPFailureKind.CREDENTIALS_REJECTED: "认证被拒",
    MCPFailureKind.NOT_FOUND: "服务地址无效",
    MCPFailureKind.UNREACHABLE: "连接失败",
    MCPFailureKind.COMMAND_NOT_FOUND: "找不到命令",
    MCPFailureKind.TIMEOUT: "连接超时",
    MCPFailureKind.OTHER: "调用失败",
}


@dataclass(frozen=True)
class MCPFailure:
    """一次失败的归类结果。``detail`` 是给日志的原始说明；``variables`` 是与这次失败有关的变量名。"""

    kind: MCPFailureKind
    server: ScopedMCPServer
    detail: str
    status_code: int | None = None
    variables: tuple[str, ...] = ()

    @classmethod
    def from_exception(cls, exc: BaseException, server: ScopedMCPServer) -> MCPFailure:
        """按「下一步该做什么」从异常（含异常组里的每个叶子）归类；认不出的归为 ``OTHER``。"""

        leaves = list(cls._leaves(exc))
        detail = describe_exception(exc)

        def first(kind: type[BaseException]) -> BaseException | None:
            return next((leaf for leaf in leaves if isinstance(leaf, kind)), None)

        if (missing := first(MissingVariablesError)) is not None:
            return cls(
                MCPFailureKind.MISSING_VARIABLES,
                server,
                detail,
                variables=tuple(missing.names),  # type: ignore[attr-defined]
            )
        if first(MCPAuthorizationRequired) is not None:
            return cls(MCPFailureKind.AUTHORIZATION_REQUIRED, server, detail)
        if first(OAuthCallbackTimeout) is not None:
            return cls(MCPFailureKind.AUTHORIZATION_TIMEOUT, server, detail)
        if (denied := first(OAuthCallbackError)) is not None:
            return cls(MCPFailureKind.AUTHORIZATION_DENIED, server, str(denied))
        if (http := first(httpx.HTTPStatusError)) is not None:
            code = http.response.status_code  # type: ignore[attr-defined]
            if code in (401, 403):
                return cls(
                    MCPFailureKind.CREDENTIALS_REJECTED,
                    server,
                    detail,
                    status_code=code,
                    variables=cls._header_variables(server),
                )
            kind = MCPFailureKind.NOT_FOUND if code == 404 else MCPFailureKind.OTHER
            return cls(kind, server, detail, status_code=code)
        if first(FileNotFoundError) is not None and server.config.transport == "stdio":
            return cls(MCPFailureKind.COMMAND_NOT_FOUND, server, detail)
        if first(httpx.TransportError) is not None or first(ConnectionError) is not None:
            return cls(MCPFailureKind.UNREACHABLE, server, detail)
        if first(TimeoutError) is not None:
            return cls(MCPFailureKind.TIMEOUT, server, detail)
        return cls(MCPFailureKind.OTHER, server, detail)

    def for_user(self) -> str:
        """配置页的说法：说清原因和该去改哪里。"""

        names = "、".join(self.variables)
        match self.kind:
            case MCPFailureKind.MISSING_VARIABLES:
                return f"引用的环境变量未设置：{names}，在卡片上填写后重试"
            case MCPFailureKind.AUTHORIZATION_REQUIRED:
                return "需要登录授权"
            case MCPFailureKind.AUTHORIZATION_TIMEOUT:
                return "等待浏览器授权超时，请重新点「登录授权」"
            case MCPFailureKind.AUTHORIZATION_DENIED:
                return f"授权未完成：{self.detail}"
            case MCPFailureKind.CREDENTIALS_REJECTED:
                where = f"，请检查变量 {names} 的值" if names else "，请检查请求头里的认证信息"
                return f"服务拒绝了认证信息（{self.status_code}）{where}"
            case MCPFailureKind.NOT_FOUND:
                return "服务地址不存在（404），请检查 URL"
            case MCPFailureKind.UNREACHABLE:
                return f"连不上服务：{self.detail}"
            case MCPFailureKind.COMMAND_NOT_FOUND:
                return f"找不到启动命令 {self.server.config.command}，请确认已安装并在 PATH 中"
            case MCPFailureKind.TIMEOUT:
                if self.server.config.transport == "stdio":
                    return "连接超时（首次启动可能在下载依赖，可稍后重试）"
                return "连接超时"
            case MCPFailureKind.OTHER:
                return self.detail

    @property
    def needs_sign_in(self) -> bool:
        """下一步是让用户点「登录授权」：配置页据此给出按钮，前端不按归类名自行判断。"""

        return self.kind in _SIGN_IN_KINDS

    @property
    def invalidates_test(self) -> bool:
        """这次失败是否说明这份定义在本机用不了：是就撤销测试记录（Agent 不再看到这个服务）。

        超时、连不上多半是一时的；需要登录说明缺的是凭据，Agent 照样能看到它、调用时得到「请用户登录」。
        撤销这些会让一次网络抖动就让 Agent 失去服务，用户还得重测。
        """

        return self.kind not in _KEEPS_TEST_KINDS

    def status_hint(self) -> str:
        """调用行的短提示（≤8 字）：与配置页同一份归类，只说原因的类别，细节在配置页与给模型的说法里。"""

        return _STATUS_HINTS[self.kind]

    def for_model(self) -> str:
        """给模型的说法：说清原因，以及应不应该重试、要不要请用户处理。"""

        name = self.server.name
        names = ", ".join(self.variables)
        match self.kind:
            case MCPFailureKind.MISSING_VARIABLES:
                return (
                    f"MCP server {name} needs environment variables that are not set: {names}. "
                    "Ask the user to set them in cosir's MCP settings, then retry."
                )
            case kind if kind in _SIGN_IN_KINDS:
                return (
                    f"MCP server {name} needs the user to sign in. Ask the user to open cosir's "
                    "MCP settings and use 'Sign in' on this server, then retry."
                )
            case MCPFailureKind.CREDENTIALS_REJECTED:
                where = f" (check the value of {names})" if names else ""
                return (
                    f"MCP server {name} rejected its credentials (HTTP {self.status_code}){where}. "
                    "Ask the user to fix them in cosir's MCP settings; retrying will not help."
                )
            case MCPFailureKind.NOT_FOUND:
                return f"MCP server {name} URL was not found (HTTP 404). Ask the user to check it."
            case MCPFailureKind.COMMAND_NOT_FOUND:
                return (
                    f"MCP server {name} could not start: command "
                    f"{self.server.config.command!r} was not found. Ask the user to install it."
                )
            case MCPFailureKind.TIMEOUT:
                # 只来自建连或列目录（调用本身的时限由 ``mcp_tools`` 另行说明），两者都没有副作用。
                return (
                    f"MCP server {name} did not respond in time (a stdio server may still be "
                    "starting); the request was not completed. Retry once."
                )
            case MCPFailureKind.UNREACHABLE | MCPFailureKind.OTHER:
                return (
                    f"MCP server {name} failed: {self.detail}. "
                    "Check whether the server is running, then retry once."
                )

    @classmethod
    def _leaves(cls, exc: BaseException) -> Iterator[BaseException]:
        if isinstance(exc, BaseExceptionGroup):
            for inner in exc.exceptions:
                yield from cls._leaves(inner)
        else:
            yield exc


    @staticmethod
    def _header_variables(server: ScopedMCPServer) -> tuple[str, ...]:
        """凭据被拒时值得检查的变量：请求头里引用的那些（静态 token 一般写成 ``Bearer ${TOKEN}``）。"""

        names = [
            reference.name
            for value in server.config.headers.values()
            for reference in find_references(value)
        ]
        return tuple(dict.fromkeys(names))
