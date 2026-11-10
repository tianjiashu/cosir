"""Task 类型规则（``models.task_kind``）决定各类 Task 固化的工具面，并守住规则表的扩展点。

对真实注册表断言：需要用户界面来自工具自己的声明（``needs_user_surface`` / ``user_decision``），
新增工具不必改任何清单；派生工具是封闭集合，按工具名登记在 ``core.tool.derivation``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.configuration import get_tool_registry
from app.config.settings import Settings
from app.core.tool.multi_agent.derivation import DERIVATIONS
from app.core.tool.multi_agent.agent_team.agent_team_preparation_service import (
    materialize_node_tools,
)
from app.core.tool.multi_agent.task_tool_scope import banned_derivation_tools, tools_for_task
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.models.task_kind import TASK_KINDS, USER_FACING_TASK_TYPES, TaskType

pytestmark = pytest.mark.usefixtures("backend_env")

# 只能给用户能操作的对话：需要用户作答的 HITL / 交接、配置草稿，以及建 Team（要审批）。
_USER_SURFACE_ONLY = {
    "agent_team",
    "browser_handoff",
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
    assert {"user", "user_fork", "agent_fork"} == USER_FACING_TASK_TYPES
    definitions = get_tool_registry().get_all_definitions()
    assert {rule.tool for rule in DERIVATIONS.values()} <= {tool.name for tool in definitions}
    assert all(
        tool.requires_user_surface for tool in definitions if tool.user_decision is not None
    )


def test_sidebar_conversations_get_every_tool() -> None:
    everything = set(get_tool_registry().get_all_tool_names())
    assert _names("user") == _names("user_fork") == everything
    assert _names("agent_fork") == everything - {"fork_task"}


@pytest.mark.parametrize("task_type", ["agent_task", "team_node_task"])
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
        allowed_tools=["read_file", "agent_team", "terminal_read", "delegate_task", "ghost"]
    )
    kept = materialize_node_tools(profile, depth=0)  # type: ignore[arg-type]
    assert kept == ["read_file", "delegate_task", "ghost"]


_DERIVATION_TOOLS = {"delegate_task", "fork_task", "agent_team"}


@pytest.mark.parametrize("task_type", ["user", "user_fork", "agent_fork", "agent_task", "team_node_task"])
def test_tasks_at_the_depth_limit_get_no_derivation_tools(monkeypatch, task_type: str) -> None:
    """深度到上限的 Task 创建时就拿不到派生工具；没到时按类型规则给，等待与通信工具照常保留。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    assert "delegate_task" in _names(task_type, depth=1)
    at_limit = _names(task_type, depth=2)
    assert not at_limit & _DERIVATION_TOOLS
    assert {"wait_messages", "read_file"} <= at_limit


def test_agent_fork_cannot_fork_but_keeps_the_other_derivations(monkeypatch) -> None:
    """分叉的派生限制走准入：Agent fork 少 ``fork_task``，到上限全禁，用户手动分叉不受限。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    assert banned_derivation_tools(TaskType.AGENT_FORK, 1) == {"fork_task"}
    assert banned_derivation_tools(TaskType.AGENT_FORK, 2) == _DERIVATION_TOOLS
    assert banned_derivation_tools(TaskType.USER_FORK, 0) == frozenset()


def test_team_nodes_share_the_main_task_depth_and_lose_delegation_at_the_limit(monkeypatch) -> None:
    """节点与主 Task 同层：主 Task 已到上限时节点连委派也没有。"""
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    profile = SimpleNamespace(allowed_tools=["read_file", "delegate_task"])
    assert materialize_node_tools(profile, depth=1) == ["read_file", "delegate_task"]  # type: ignore[arg-type]
    assert materialize_node_tools(profile, depth=2) == ["read_file"]  # type: ignore[arg-type]


def test_task_type_enum_covers_rules_and_response_schema():
    from app.api.schemas.response.TaskResponse import TaskResponse
    assert set(TASK_KINDS) == set(TaskType)
    assert all(isinstance(rule.creates, TaskType) for rule in DERIVATIONS.values())
    schema = TaskResponse.model_json_schema()
    assert set(schema["$defs"]["TaskType"]["enum"]) == {kind.value for kind in TaskType}
    with pytest.raises(ValueError):
        TaskType("fork")
