"""Task 类型规则（``models.task_kind``）决定各类 Task 固化的工具面，并守住规则表的扩展点。

对真实注册表断言：需要用户界面来自工具自己的声明（``needs_user_surface`` / ``user_decision``），
新增工具不必改任何清单；派生工具是封闭集合，按工具名登记在 ``core.tool.derivation``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.configuration import get_tool_registry
from app.config.settings import Settings
from app.core.tool.derivation import DERIVATIONS
from app.core.tool.handler.agent_team.agent_team_preparation_service import (
    materialize_node_tools,
)
from app.core.tool.task_tool_scope import fork_tool_schemas, tools_for_task
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.models.task_kind import TASK_KINDS, USER_FACING_TASK_TYPES

pytestmark = pytest.mark.usefixtures("backend_env")

# 只能给用户能操作的对话：需要用户作答的 HITL / 交接、配置草稿，以及建 Team（要审批）。
_USER_SURFACE_ONLY = {
    "agent_team",
    "browser_handoff",
    "terminal_handoff",
    "propose_agent_configuration",
    "propose_agent_team_configuration",
}


def _names(task_type: str, depth: int = 0) -> set[str]:
    definitions = get_tool_registry().get_all_definitions()
    return {tool.name for tool in tools_for_task(task_type, definitions, depth=depth)}


def _terminal_session_tools() -> set[str]:
    return set(get_tool_registry().tool_groups_to_tool_names([TOOL_GROUP_TERMINAL_SESSION]))


def test_rule_table_is_closed_over_its_own_references() -> None:
    """扩展守卫：派生建出的类型都已登记、HITL 工具都只给侧栏。

    派生工具都在注册表里：工具改名时这里失败，而不是过滤悄悄失效。
    """
    assert {rule.creates for rule in DERIVATIONS.values()} <= set(TASK_KINDS)
    assert {"user", "fork"} == USER_FACING_TASK_TYPES
    definitions = get_tool_registry().get_all_definitions()
    assert {rule.tool for rule in DERIVATIONS.values()} <= {tool.name for tool in definitions}
    assert all(
        tool.requires_user_surface for tool in definitions if tool.user_decision is not None
    )


def test_sidebar_conversations_get_every_tool() -> None:
    everything = set(get_tool_registry().get_all_tool_names())
    assert _names("user") == _names("fork") == everything


@pytest.mark.parametrize("task_type", ["delegate_task", "agent_team_node"])
def test_read_only_tasks_can_delegate_but_not_use_user_surfaces(task_type: str) -> None:
    """委派子 Agent 与 Team 节点只读：去掉需要用户界面的工具与建 Team，委派与等待照常保留。"""
    names = _names(task_type)
    assert not names & (_USER_SURFACE_ONLY | _terminal_session_tools())
    assert {"delegate_task", "send_message", "task_status", "wait_messages"} <= names
    # fork 出的是侧栏对话：只读 Task 不能 fork。
    assert "fork_task" not in names
    assert {"read_file", "execute_terminal", "browser_navigate"} <= names


def test_unknown_task_type_is_an_assembly_defect() -> None:
    with pytest.raises(KeyError):
        tools_for_task("mystery", [], depth=0)


def test_team_node_tools_filter_by_rule_and_keep_unregistered_names() -> None:
    """节点工具按规则过滤、不代为追加；注册表里没有的名字原样保留，让节点建 Task 时如实报错。"""
    profile = SimpleNamespace(
        allowed_tools=["read_file", "agent_team", "terminal_start", "delegate_task", "ghost"]
    )
    kept = materialize_node_tools(profile, depth=0)  # type: ignore[arg-type]
    assert kept == ["read_file", "delegate_task", "ghost"]


_DERIVATION_TOOLS = {"delegate_task", "fork_task", "agent_team"}


@pytest.mark.parametrize("task_type", ["user", "fork", "delegate_task", "agent_team_node"])
def test_tasks_at_the_depth_limit_get_no_derivation_tools(monkeypatch, task_type: str) -> None:
    """深度到上限的 Task 创建时就拿不到派生工具；没到时按类型规则给，等待与通信工具照常保留。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    assert "delegate_task" in _names(task_type, depth=1)
    at_limit = _names(task_type, depth=2)
    assert not at_limit & _DERIVATION_TOOLS
    assert {"wait_messages", "read_file"} <= at_limit


def _schemas(*names: str) -> list[dict[str, object]]:
    return [{"name": name} for name in names]


def test_fork_keeps_the_source_schemas_and_drops_derivation_at_the_limit(monkeypatch) -> None:
    """分叉沿用源的 schema（顺序不变，请求前缀与源一致）；新深度到上限时去掉派生工具。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    source = _schemas("read_file", "delegate_task", "fork_task", "agent_team", "wait_messages")
    assert fork_tool_schemas(source, source_depth=0, depth=1) == source
    assert fork_tool_schemas(source, source_depth=1, depth=2) == _schemas(
        "read_file", "wait_messages"
    )


def test_user_fork_of_a_limited_fork_gets_the_derivation_tools_back(monkeypatch) -> None:
    """用户手动分叉深度归 0：源因深度没有的派生工具按主 Agent 规则补到末尾；不比源浅时不补。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    source = _schemas("read_file", "wait_messages")

    restored = fork_tool_schemas(source, source_depth=2, depth=0)

    assert restored[:2] == source
    assert {schema["name"] for schema in restored[2:]} == _DERIVATION_TOOLS
    assert fork_tool_schemas(source, source_depth=0, depth=0) == source


def test_team_nodes_share_the_main_task_depth_and_lose_delegation_at_the_limit(monkeypatch) -> None:
    """节点与主 Task 同层：主 Task 已到上限时节点连委派也没有。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    profile = SimpleNamespace(allowed_tools=["read_file", "delegate_task"])
    assert materialize_node_tools(profile, depth=1) == ["read_file", "delegate_task"]  # type: ignore[arg-type]
    assert materialize_node_tools(profile, depth=2) == ["read_file"]  # type: ignore[arg-type]
