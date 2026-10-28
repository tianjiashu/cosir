"""交互终端只开放给侧栏对话 Task 的主 Agent：子 Agent 与 Team 节点按工具组整体剥离。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.configuration import get_tool_registry
from app.core.tool.handler.agent_team.agent_team_preparation_service import (
    materialize_node_tools,
)
from app.core.tool.handler.child_agent_create import strip_child_disallowed_tools
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION

pytestmark = pytest.mark.usefixtures("backend_env")


def _terminal_tool_names() -> set[str]:
    return set(get_tool_registry().tool_groups_to_tool_names([TOOL_GROUP_TERMINAL_SESSION]))


def test_terminal_group_is_registered_and_includes_list() -> None:
    names = _terminal_tool_names()
    assert {"terminal_start", "terminal_write", "terminal_read", "terminal_list"} <= names


def test_child_agent_schema_has_no_terminal_tools() -> None:
    kept = strip_child_disallowed_tools(get_tool_registry().get_all_definitions())
    kept_names = {tool.name for tool in kept}
    assert not kept_names & _terminal_tool_names()
    assert not any(name.startswith("terminal_") for name in kept_names)
    # 非交互终端的工具照常保留。
    assert "read_file" in kept_names


def test_team_node_tools_have_no_terminal_tools() -> None:
    all_names = get_tool_registry().get_all_tool_names()
    # 只读 allowed_tools：用鸭子类型替身，不构造完整 Profile。
    profile = SimpleNamespace(allowed_tools=list(all_names))
    node_tools = materialize_node_tools(profile)  # type: ignore[arg-type]
    assert not set(node_tools) & _terminal_tool_names()
    assert not any(name.startswith("terminal_") for name in node_tools)
    # Team 节点保留委派工具（与子 Agent 清单的唯一差别）。
    assert "delegate_task" in node_tools
