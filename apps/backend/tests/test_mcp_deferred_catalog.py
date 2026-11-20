"""MCP 作为延迟工具来源：模型可见名、本机记录里的工具目录、目录的缓存与失效。

真实连接服务的调用路径在 ``test_mcp_skill_tools`` / ``test_mcp_connection_pool``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.tool.deferred.deferred_tools import DeferredCatalogUnavailable
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import MCPServerConfig, ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState, MCPToolEntry
from app.tools.mcp import deferred_catalog
from app.tools.mcp.deferred_catalog import MCPDeferredTools
from app.tools.mcp.tool_naming import model_tool_names

_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _entry(name: str, description: str = "d") -> MCPToolEntry:
    return MCPToolEntry(name=name, description=description, input_schema=_SCHEMA)


def _configure(workspace: Path, *configs: MCPServerConfig) -> list[ScopedMCPServer]:
    path = workspace / ".cosir" / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": [c.model_dump(mode="json") for c in configs]}), encoding="utf-8")
    return [ScopedMCPServer(ExtensionScope.workspace(workspace), c) for c in configs]


# —— 命名 ——


def test_names_are_prefixed_sanitized_and_unique() -> None:
    long_tool = "x" * 80
    names = model_tool_names(
        [("git hub", "create.issue"), ("a__b", "c"), ("a", "b__c"), ("srv", long_tool)]
    )
    assert names[("git hub", "create.issue")] == "mcp__git_hub__create_issue"
    # 「a__b / c」与「a / b__c」拼出同一个名字：两边都带哈希，谁也不占原名（与顺序无关）。
    assert names[("a__b", "c")] != names[("a", "b__c")]
    assert all(n.startswith("mcp__a__b__c_") for n in (names[("a__b", "c")], names[("a", "b__c")]))
    assert len(names[("srv", long_tool)]) == 64
    assert model_tool_names([("srv", long_tool)]) == {("srv", long_tool): names[("srv", long_tool)]}


# —— 本机记录里的目录 ——


def test_catalog_lives_and_dies_with_the_test_record(tmp_path: Path) -> None:
    scope = ExtensionScope.workspace(tmp_path)
    saved = ScopedMCPServer(scope, MCPServerConfig(name="one", command="python"))
    draft = ScopedMCPServer(scope, MCPServerConfig(name="one", command="node"))
    MCPMachineState.record_pass(saved, [_entry("search")])
    MCPMachineState.record_pass(draft, [_entry("draft_only")])

    assert [e.name for e in MCPMachineState.read().catalog(saved)] == ["search"]
    # 保存配置裁到当前定义：草稿的目录随草稿的测试记录一起丢掉。
    MCPMachineState.keep_only(scope, [saved])
    assert MCPMachineState.read().catalog(draft) == []
    assert [e.name for e in MCPMachineState.read().catalog(saved)] == ["search"]
    MCPMachineState.revoke(saved)
    assert MCPMachineState.read().catalog(saved) == []


def test_pool_write_back_only_updates_tested_definitions(tmp_path: Path) -> None:
    scope = ExtensionScope.workspace(tmp_path)
    tested = ScopedMCPServer(scope, MCPServerConfig(name="one", command="python"))
    untested = ScopedMCPServer(scope, MCPServerConfig(name="two", command="python"))
    MCPMachineState.record_pass(tested, [_entry("old")])

    MCPMachineState.record_catalog(tested, [_entry("old"), _entry("new")])
    MCPMachineState.record_catalog(untested, [_entry("sneaky")])

    record = MCPMachineState.read()
    assert [e.name for e in record.catalog(tested)] == ["old", "new"]
    assert record.catalog(untested) == []


# —— 延迟工具目录 ——


def test_catalog_lists_available_servers_and_follows_file_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    one, disabled = _configure(
        workspace,
        MCPServerConfig(name="one", command="python"),
        MCPServerConfig(name="off", command="python", enabled=False),
    )
    MCPMachineState.record_pass(one, [_entry("search", "Find  things\nfast")])
    MCPMachineState.record_pass(disabled, [_entry("hidden")])
    source = MCPDeferredTools()

    [search] = source.catalog(str(workspace))
    assert search.name == "mcp__one__search"
    assert search.description == "[MCP server one] Find things fast"
    assert search.parameters == _SCHEMA

    # 本机记录变了（连接池回写了新工具）：签名变化，下一次读取就看到。
    MCPMachineState.record_catalog(one, [_entry("search"), _entry("fetch")])
    assert [d.name for d in source.catalog(str(workspace))] == ["mcp__one__fetch", "mcp__one__search"]


def test_catalog_reuses_the_projection_while_files_are_unchanged(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    [one] = _configure(workspace, MCPServerConfig(name="one", command="python"))
    MCPMachineState.record_pass(one, [_entry("search")])
    source = MCPDeferredTools()
    first = source.catalog(str(workspace))
    monkeypatch.setattr(deferred_catalog, "_load", lambda _root: pytest.fail("must not reload"))

    assert source.catalog(str(workspace)) is first


def test_unreadable_configuration_makes_the_catalog_unavailable(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    path = workspace / ".cosir" / "config" / "mcp_servers.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(DeferredCatalogUnavailable, match="could not be read"):
        MCPDeferredTools().catalog(str(workspace))
