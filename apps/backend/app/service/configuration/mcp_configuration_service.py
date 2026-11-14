"""一个作用域（系统级或 workspace 级）的 MCP 服务配置读写与连接测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.configuration.mcp_configuration import (
    MCPConfiguration,
    MCPConfigurationError,
    MCPServerConfig,
    ScopedMCPServer,
    available_servers,
    read_mcp_configuration,
    server_connection_fingerprint,
    visible_servers,
)
from app.service.configuration.mcp_tested_servers import MCPTestedServers
from app.service.configuration.runtime_notices.catalog_change import (
    CatalogChange,
    broadcast_catalog_changes,
)
from app.tools.mcp.client import describe_exception, list_server_tools

MCP_TEST_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class MCPServerTestResult:
    """一次 MCP 连接测试返回的工具目录或错误。

    ``agent_available`` 表示本作用域已保存的同名服务就是这份连接配置：测试通过后它立即可供 Agent
    使用（仍受启用开关约束）；为 ``False`` 时说明测的是还没保存的草稿，保存同一配置后即可用。
    """

    success: bool
    tools: list[dict[str, object]]
    error: str | None = None
    agent_available: bool = False


class MCPConfigurationService:
    """读写一个作用域的 MCP 服务配置，并以短连接测试服务；``workspace_root`` 为空即系统级。"""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        self.scope = ExtensionScope(workspace_root)
        self.store = ConfigurationFileStore()

    def visible(self) -> list[ScopedMCPServer]:
        """本作用域配置页看得到的服务（workspace 级含继承的系统服务）。"""

        return visible_servers(self.scope.workspace_root)

    def update(self, servers: list[MCPServerConfig]) -> list[ScopedMCPServer]:
        """整表替换本作用域的服务列表，返回更新后的可见服务。

        只写本作用域的文件。测试记录随之裁到「仍存在的服务的当前配置」：连接参数没变的服务照旧
        可用，保存的正是测过的草稿时也立即可用；删掉的服务与改掉的旧参数要重新测试。
        """

        names = [server.name for server in servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP 服务名称必须唯一")
        serialized = (
            json.dumps(
                MCPConfiguration(servers=servers).model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        with self.store.locked(self.scope.mcp_servers_file):
            before = self._agent_view()
            self.store.write_text_atomic(
                self.scope.mcp_servers_file, serialized, root=self.scope.cosir_dir
            )
            MCPTestedServers.keep_only(
                self.scope, [ScopedMCPServer(self.scope, server) for server in servers]
            )
        self._publish(before)
        log.info(
            "configuration_mcp_servers_written",
            extra={
                "msg": "MCP 服务配置已保存",
                "data": {"scope": self.scope.key, "server_count": len(servers)},
            },
        )
        return self.visible()

    async def test(self, config: MCPServerConfig) -> MCPServerTestResult:
        """短连接并发现工具；成功即记下本机测试记录，失败撤销同一配置的旧记录。

        测的可以是还没保存的草稿：记录按连接指纹保存，之后保存同一配置即可用，不用再测一次。
        连接失败作为测试结果返回，供配置界面展示。
        """

        server = ScopedMCPServer(self.scope, config)
        try:
            async with asyncio.timeout(MCP_TEST_TIMEOUT_SECONDS):
                tools = await list_server_tools(server)
        except TimeoutError:
            self._revoke_and_publish(server)
            log.warning(
                "configuration_mcp_server_test_timed_out",
                extra={
                    "msg": "MCP 服务连接测试超时",
                    "data": {"scope": self.scope.key, "server_name": config.name},
                },
            )
            return MCPServerTestResult(success=False, tools=[], error="连接测试超时")
        except Exception as exc:
            self._revoke_and_publish(server)
            log.warning(
                "configuration_mcp_server_test_failed",
                extra={
                    "msg": "MCP 服务连接测试失败",
                    "data": {
                        "scope": self.scope.key,
                        "server_name": config.name,
                        "error": describe_exception(exc),
                    },
                },
            )
            return MCPServerTestResult(success=False, tools=[], error=describe_exception(exc))
        before = self._agent_view()
        MCPTestedServers.record_pass(server)
        self._publish(before)
        return MCPServerTestResult(
            success=True, tools=tools, agent_available=self._is_saved(server)
        )

    def _is_saved(self, server: ScopedMCPServer) -> bool:
        """本作用域已保存的同名服务是否就是这份连接配置。

        已保存的配置文件读不出（损坏）时按「不是」回答：测试本身已经成功并记下，用户修好文件、
        保存同一配置后即可用，不能因为旧文件坏了让这次测试报 500。
        """

        try:
            saved = read_mcp_configuration(self.scope).servers
        except MCPConfigurationError:
            return False
        return any(
            item.name == server.name and server_connection_fingerprint(item) == server.fingerprint
            for item in saved
        )

    def _revoke_and_publish(self, server: ScopedMCPServer) -> None:
        before = self._agent_view()
        if MCPTestedServers.revoke(server):
            self._publish(before)
            log.info(
                "configuration_mcp_server_test_revoked",
                extra={
                    "msg": "MCP 服务重测失败，已撤销 Agent 可用状态",
                    "data": {"scope": self.scope.key, "server_name": server.name},
                },
            )

    def _agent_view(self) -> dict[str, str | None] | None:
        """Agent 在本作用域看到的服务名（目录只列名字，描述为 ``None``）；配置读不出时为 ``None``。

        读不出（文件损坏）时不比较也不通知：用户正是要经保存修好它，不能因为旧文件坏了就拒绝保存。
        """

        try:
            return {server.name: None for server in available_servers(self.scope.workspace_root)}
        except MCPConfigurationError:
            return None

    def _publish(self, before: dict[str, str | None] | None) -> None:
        """可用服务集合变化时通知固化了 ``mcp`` 工具的 Session。"""

        after = self._agent_view()
        if before is None or after is None:
            return
        broadcast_catalog_changes(
            CatalogChange.between(
                "mcp_servers",
                scope=None if self.scope.workspace_root is None else self.scope.key,
                before=before,
                after=after,
                shadowed_in=_workspace_defines if self.scope.workspace_root is None else None,
            )
        )


def _workspace_defines(workspace_root: str, name: str) -> bool:
    """工作区自己的配置里是否有同名服务（启用与否都会遮住系统级同名服务）。"""

    try:
        configuration = read_mcp_configuration(ExtensionScope.workspace(workspace_root))
    except MCPConfigurationError:
        return False
    return any(server.name == name for server in configuration.servers)
