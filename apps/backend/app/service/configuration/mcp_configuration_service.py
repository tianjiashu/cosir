"""系统级 MCP 服务配置的读写与连接测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.mcp.mcp_client import (
    MCPConfiguration,
    MCPServerConfig,
    list_server_tools,
    read_mcp_configuration,
    server_connection_fingerprint,
)
from app.utils.path.system_cosir import system_cosir_dir, system_mcp_servers_file

MCP_TEST_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class MCPServerTestResult:
    """一次 MCP 连接测试返回的工具目录或错误。"""

    success: bool
    tools: list[dict[str, object]]
    error: str | None = None
    agent_available: bool = False


class MCPConfigurationService:
    """读写系统 MCP 配置，并通过短连接测试未保存的服务配置。"""

    def __init__(self) -> None:
        self.path = system_mcp_servers_file()
        self.root = system_cosir_dir()
        self.store = ConfigurationFileStore()

    def read(self) -> MCPConfiguration:
        """读取系统配置；文件缺失时返回空服务列表。"""

        return read_mcp_configuration()

    def update(self, configuration: MCPConfiguration) -> MCPConfiguration:
        """保存服务列表；只有连接参数未变化的服务保留既有成功测试状态。"""

        names = [server.name for server in configuration.servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP 服务名称必须唯一")
        previous = self.read()
        tested_servers = {
            server.name: server_connection_fingerprint(server)
            for server in configuration.servers
            if previous.tested_servers.get(server.name) == server_connection_fingerprint(server)
        }
        configuration = MCPConfiguration(
            servers=configuration.servers,
            tested_servers=tested_servers,
        )
        self._write(configuration)
        log.info(
            "configuration_mcp_servers_written",
            extra={
                "msg": "MCP 服务配置已保存",
                "data": {"server_count": len(configuration.servers)},
            },
        )
        return configuration

    def _write(self, configuration: MCPConfiguration) -> None:
        """原子写入 MCP 配置事实。"""

        serialized = (
            json.dumps(configuration.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        )
        self.store.write_text_atomic(self.path, serialized, root=self.root)

    async def test(self, server: MCPServerConfig) -> MCPServerTestResult:
        """短连接并发现工具；连接失败作为测试结果返回，供配置界面展示。"""

        try:
            async with asyncio.timeout(MCP_TEST_TIMEOUT_SECONDS):
                tools = await list_server_tools(server)
        except TimeoutError:
            self._clear_test_status(server)
            log.warning(
                "configuration_mcp_server_test_timed_out",
                extra={
                    "msg": "MCP 服务连接测试超时",
                    "data": {"server_name": server.name},
                },
            )
            return MCPServerTestResult(success=False, tools=[], error="连接测试超时")
        except Exception as exc:
            self._clear_test_status(server)
            log.warning(
                "configuration_mcp_server_test_failed",
                extra={
                    "msg": "MCP 服务连接测试失败",
                    "data": {"server_name": server.name, "error_type": type(exc).__name__},
                },
            )
            return MCPServerTestResult(success=False, tools=[], error=str(exc))
        configuration = self.read()
        saved_server = next(
            (item for item in configuration.servers if item.name == server.name), None
        )
        agent_available = saved_server is not None and server_connection_fingerprint(
            saved_server
        ) == server_connection_fingerprint(server)
        if agent_available:
            tested_servers = dict(configuration.tested_servers)
            tested_servers[server.name] = server_connection_fingerprint(server)
            self._write(
                MCPConfiguration(
                    servers=configuration.servers,
                    tested_servers=tested_servers,
                )
            )
        return MCPServerTestResult(success=True, tools=tools, agent_available=agent_available)

    def _clear_test_status(self, server: MCPServerConfig) -> None:
        """匹配已保存连接配置时撤销旧测试结果，避免失败重测后继续向 Agent 暴露。"""

        configuration = self.read()
        saved_server = next(
            (item for item in configuration.servers if item.name == server.name), None
        )
        if (
            saved_server is None
            or server_connection_fingerprint(saved_server) != server_connection_fingerprint(server)
            or server.name not in configuration.tested_servers
        ):
            return

        tested_servers = dict(configuration.tested_servers)
        tested_servers.pop(server.name)
        self._write(
            MCPConfiguration(
                servers=configuration.servers,
                tested_servers=tested_servers,
            )
        )
        log.info(
            "configuration_mcp_server_test_revoked",
            extra={
                "msg": "MCP 服务重测失败，已撤销 Agent 可用状态",
                "data": {"server_name": server.name},
            },
        )
