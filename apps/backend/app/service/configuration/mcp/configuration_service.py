"""一个作用域（系统级或 workspace 级）的 MCP 服务配置读写、连接测试与登录授权。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.service.configuration import file_store
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import (
    MCPConfiguration,
    MCPConfigurationError,
    MCPServerConfig,
    ScopedMCPServer,
    visible_servers,
)
from app.service.configuration.mcp.configuration_view import MCPServerEntry
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.service.configuration.mcp.oauth_flows import (
    AUTHORIZATION_TIMEOUT_SECONDS,
    MCPAuthorizationFlows,
    PendingAuthorization,
)
from app.tools.mcp.client import list_server_tools
from app.tools.mcp.failure import MCPFailure
from app.tools.mcp.oauth.provider import AuthorizationInteraction

# 一次连接测试的时限。本地 stdio 服务首次用 ``npx`` / ``uvx`` 启动要先下载依赖，常常超过二十秒；
# 时限只影响失败的情况，成功的测试一连上就返回，所以不按传输方式另设。
MCP_TEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class MCPServerTestResult:
    """一次连接测试（或登录授权）的结果：成功时带工具目录，失败时带归类后的原因。

    ``failure_kind`` 是失败归类（``MCPFailureKind``）；``needs_sign_in`` 是归类给出的下一步「登录授权」
    （``MCPFailure.needs_sign_in``），配置页据此显示按钮，不按归类名自行判断。
    ``agent_available`` 表示本作用域已保存的同名服务就是这份连接配置：测试通过后它立即可供 Agent
    使用（仍受启用开关约束）；为 ``False`` 时说明测的是还没保存的草稿，保存同一配置后即可用。
    """

    success: bool
    tools: list[dict[str, object]]
    error: str | None = None
    failure_kind: str | None = None
    needs_sign_in: bool = False
    agent_available: bool = False


# 配置页「登录授权」进行中的交互流程（进程内协调状态，不跨重启）。
_authorization_flows = MCPAuthorizationFlows()


class MCPConfigurationService:
    """读写一个作用域的 MCP 服务配置，并以短连接测试服务；``workspace_root`` 为空即系统级。"""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.scope = ExtensionScope(workspace_root)

    def visible(self) -> list[MCPServerEntry]:
        """本作用域配置页看得到的服务（workspace 级含继承的系统服务）及其本机状态。"""

        return MCPServerEntry.for_servers(visible_servers(self.scope.workspace_root))

    def update(self, servers: list[MCPServerConfig]) -> list[MCPServerEntry]:
        """整表替换本作用域的服务列表，返回更新后的可见服务。

        只写本作用域的文件。本机记录随之裁到「仍存在的服务的当前配置」（``MCPMachineState.keep_only``）：
        连接参数没变的服务照旧可用，保存的正是测过的草稿时也立即可用；删掉的服务与改掉的旧参数要重新测试，
        删掉的服务连同授权一起丢掉。

        异常:
            MCPConfigurationError: 服务名称重复。
        """

        configuration = MCPConfiguration.of(servers)
        with file_store.locked(self.scope.mcp_servers_file):
            file_store.write_json(
                self.scope.mcp_servers_file,
                configuration,
                root=self.scope.cosir_dir,
            )
            MCPMachineState.keep_only(
                self.scope, [ScopedMCPServer(self.scope, server) for server in servers]
            )
        log.info(
            "configuration_mcp_servers_written",
            extra={
                "msg": "MCP 服务配置已保存",
                "data": {"scope": self.scope.key, "server_count": len(servers)},
            },
        )
        return self.visible()

    async def test(self, config: MCPServerConfig) -> MCPServerTestResult:
        """短连接并发现工具，从不打开浏览器；成功即记下本机测试记录，失败时按归类决定是否撤销同一定义的
        旧记录（``MCPFailure.invalidates_test``：一时的失败与缺凭据不撤销）。

        需要授权的服务在这里只得出「需要授权」（``failure_kind``），并在本机记录里标上待授权；真正登录走
        :meth:`authorize`。测试因此可以放心批量跑、保存后自动跑，不会弹出任何东西。测的可以是还没保存的
        草稿：记录按定义指纹保存，之后保存同一配置即可用。
        """

        server = self._server(config)
        try:
            async with asyncio.timeout(MCP_TEST_TIMEOUT_SECONDS):
                tools = await list_server_tools(server)
        except Exception as exc:
            return self._failed(server, MCPFailure.from_exception(exc, server))
        return self._passed(server, tools)

    async def authorize(self, config: MCPServerConfig) -> MCPServerTestResult | PendingAuthorization:
        """用户点了「登录授权」：开回调监听并连接服务，拿到授权地址就交给前端打开（两段式）。

        已有可用令牌或服务其实不要求授权时，直接返回连接结果（相当于一次测试）；授权完成后同样记下测试
        记录。结果由 :meth:`wait_authorization` 取走，放弃时 :meth:`cancel_authorization`。
        """

        server = self._server(config)
        try:
            connection = server.connection_config()
            return await _authorization_flows.begin(
                server.pool_key,
                connection.oauth.callback_port if connection.oauth is not None else None,
                lambda interaction: self._run_interactive(server, interaction),
                timeout_before_authorization=MCP_TEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return self._failed(server, MCPFailure.from_exception(exc, server))

    @staticmethod
    async def wait_authorization(flow_id: str) -> MCPServerTestResult:
        """取一次登录授权的最终结果（等用户在浏览器里完成、取消或超时）。

        异常:
            KeyError: 没有这次授权（已取走、被取消或顶替，或后端重启过）。
        """

        return await _authorization_flows.wait(flow_id)

    @staticmethod
    def cancel_authorization(flow_id: str) -> None:
        """用户放弃登录授权：结束后端等待、关掉回调监听。

        异常:
            KeyError: 没有这次授权。
        """

        _authorization_flows.cancel(flow_id)

    @staticmethod
    def cancel_authorizations(scope: ExtensionScope) -> None:
        """取消一个作用域里进行中的登录授权；删除工作区时在丢掉本机记录之前调用，只能在后端事件循环上调。"""

        _authorization_flows.cancel_scope(scope.key)

    def _server(self, config: MCPServerConfig) -> ScopedMCPServer:
        return ScopedMCPServer(self.scope, config, used_in=self.scope.workspace_root)

    async def _run_interactive(
        self, server: ScopedMCPServer, interaction: AuthorizationInteraction
    ) -> MCPServerTestResult:
        """交互授权里的那次连接，就地结算测试记录；失败收成结果，只有取消原样上浮。

        整个流程有总时限：等浏览器回调另有自己的时限（超时归为「授权超时」），这里兜住回调之后换令牌、列工具
        卡住的情况，否则流程会一直占着登记表与回调端口。
        """

        try:
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT_SECONDS + MCP_TEST_TIMEOUT_SECONDS):
                tools = await list_server_tools(server, interaction)
        except Exception as exc:
            return self._failed(server, MCPFailure.from_exception(exc, server))
        return self._passed(server, tools)

    def _passed(self, server: ScopedMCPServer, tools: list[dict[str, object]]) -> MCPServerTestResult:
        MCPMachineState.record_pass(server)
        log.info(
            "configuration_mcp_server_test_passed",
            extra={
                "msg": "MCP 服务连接测试通过",
                "data": {"scope": self.scope.key, "server_name": server.name, "tool_count": len(tools)},
            },
        )
        return MCPServerTestResult(success=True, tools=tools, agent_available=self._is_saved(server))

    def _failed(self, server: ScopedMCPServer, failure: MCPFailure) -> MCPServerTestResult:
        if failure.invalidates_test:
            self._revoke(server)
        log.warning(
            "configuration_mcp_server_test_failed",
            extra={
                "msg": "MCP 服务连接测试失败",
                "data": {
                    "scope": self.scope.key,
                    "server_name": server.name,
                    "kind": failure.kind,
                    "error": failure.detail,
                },
            },
        )
        return MCPServerTestResult(
            success=False,
            tools=[],
            error=failure.for_user(),
            failure_kind=failure.kind,
            needs_sign_in=failure.needs_sign_in,
        )

    def _is_saved(self, server: ScopedMCPServer) -> bool:
        """本作用域已保存的同名服务是否就是这份连接配置。

        已保存的配置文件读不出（损坏）时按「不是」回答：测试本身已经成功并记下，用户修好文件、
        保存同一配置后即可用，不能因为旧文件坏了让这次测试报 500。
        """

        try:
            saved = MCPConfiguration.read(self.scope).servers
        except MCPConfigurationError:
            return False
        return any(
            ScopedMCPServer(self.scope, item).definition_fingerprint == server.definition_fingerprint
            for item in saved
            if item.name == server.name
        )

    def _revoke(self, server: ScopedMCPServer) -> None:
        if MCPMachineState.revoke(server):
            log.info(
                "configuration_mcp_server_test_revoked",
                extra={
                    "msg": "MCP 服务重测失败，已撤销 Agent 可用状态",
                    "data": {"scope": self.scope.key, "server_name": server.name},
                },
            )

