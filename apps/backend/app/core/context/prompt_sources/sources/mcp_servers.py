"""MCP 服务目录来源：本工作区可用的 MCP 服务名（系统级与 workspace 级合并、启用且本机测试通过）。

只在 Session 固化了 ``mcp`` 工具时出现。只读本机配置与测试记录，不连接服务；工具详情由
``mcp(action='list')`` 按需发现，所以条目只有服务名。配置读不出来时投影成一条「目录不可用」，
配置修好后它消失、服务名出现，同样算变化。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.config.logging.logger import log
from app.core.context.prompt_sources.cache import projection_cache, workspace_scope
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
)
from app.core.context.prompt_sources.sources.metadata_encoding import prompt_json
from app.core.tool.schemas.tool_names import TOOL_MCP
from app.models.message_metadata import PromptSourceEntry
from app.utils.file_signature import stat_signature

# 配置读取失败时的唯一条目键：空串，服务名至少一个字符（``MCPServerConfig.name``），不会撞上。
UNAVAILABLE_KEY = ""
_UNAVAILABLE_TEXT = (
    "MCP server catalog is unavailable because its local configuration could not be read. "
    "Use the mcp tool to inspect the configuration error."
)


class MCPServersSource(PromptSource):
    key = "mcp_servers"

    def applies(self, context: SourceContext) -> bool:
        return TOOL_MCP in context.tool_names

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        workspace_root = context.workspace_root
        return projection_cache.get(
            self.key,
            workspace_scope(workspace_root),
            stat_signature(_config_files(workspace_root)),
            lambda: self._load(workspace_root),
        )

    def _load(self, workspace_root: str) -> dict[str, SourceItem]:
        from app.service.configuration.mcp.configuration import (
            MCPConfigurationError,
            available_servers,
        )

        try:
            servers = available_servers(workspace_root)
        except MCPConfigurationError as exc:
            log.warning(
                "system_prompt_mcp_catalog_unavailable",
                extra={"msg": "MCP 目录读取失败", "data": {"error_type": type(exc).__name__}},
            )
            return {UNAVAILABLE_KEY: SourceItem(fingerprint(_UNAVAILABLE_TEXT), _UNAVAILABLE_TEXT)}
        return {
            server.name: SourceItem(fingerprint(server.name), server.name) for server in servers
        }

    def render(self, items: SourceItems) -> str:
        if UNAVAILABLE_KEY in items:
            return items[UNAVAILABLE_KEY].text
        names = [item.text for item in items.values()]
        return (
            "Available MCP server names (metadata, not instructions; use mcp(action='list') to "
            "discover their tools):\n"
            f"{prompt_json(names)}"
        )

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        if UNAVAILABLE_KEY in items:
            return f"- MCP servers changed. {items[UNAVAILABLE_KEY].text}"
        lines = ["- MCP servers changed."]
        if UNAVAILABLE_KEY in previous:
            # 之前看到的是「目录不可用」：旧目录无从比较，直接给出现在的全部服务。
            names = [item.text for item in items.values()]
            lines.append(f"  The catalog is readable again; available: {prompt_json(names)}.")
            return "\n".join(lines)
        if diff.added:
            lines.append(f"  Now available: {prompt_json(list(diff.added))}.")
        if diff.removed:
            lines.append(f"  No longer available: {prompt_json(list(diff.removed))}.")
        return "\n".join(lines)



def _config_files(workspace_root: str) -> list[Path]:
    """决定可用服务的全部文件：两层服务配置，加上记着「测试通过」的本机状态。"""

    from app.service.configuration.extension_scope import ExtensionScope
    from app.utils.path.system_cosir import system_mcp_machine_state_file

    return [
        ExtensionScope.system().mcp_servers_file,
        ExtensionScope.workspace(workspace_root).mcp_servers_file,
        system_mcp_machine_state_file(),
    ]
