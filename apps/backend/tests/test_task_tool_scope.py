"""Task 类型规则（``models.task_kind``）决定各类 Task 固化的工具面，并守住规则表的扩展点。

对真实注册表断言：规则来自工具自己的声明（``derivation`` / ``needs_user_surface`` /
``user_decision``），新增工具不必改任何清单。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.configuration import get_tool_registry
from app.core.tool.handler.agent_team.agent_team_preparation_service import (
    materialize_node_tools,
)
from app.core.tool.task_tool_scope import tools_for_task
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.models.task_kind import DERIVATIONS, TASK_KINDS, USER_FACING_TASK_TYPES

pytestmark = pytest.mark.usefixtures("backend_env")

# 只能给用户能操作的对话：需要用户作答的 HITL / 交接、配置草稿，以及建 Team（要审批）。
_USER_SURFACE_ONLY = {
    "agent_team",
    "browser_handoff",
    "terminal_handoff",
    "propose_agent_configuration",
    "propose_agent_team_configuration",
}


def _names(task_type: str) -> set[str]:
    definitions = get_tool_registry().get_all_definitions()
    return {tool.name for tool in tools_for_task(task_type, definitions)}


def _terminal_session_tools() -> set[str]:
    return set(get_tool_registry().tool_groups_to_tool_names([TOOL_GROUP_TERMINAL_SESSION]))


def test_rule_table_is_closed_over_its_own_references() -> None:
    """扩展守卫：派生建出的类型都已登记、派生工具声明的派生方式都在表里、HITL 工具都只给侧栏。"""
    assert {rule.creates for rule in DERIVATIONS.values()} <= set(TASK_KINDS)
    assert {"user", "fork"} == USER_FACING_TASK_TYPES
    definitions = get_tool_registry().get_all_definitions()
    derivations = {tool.name: tool.derivation for tool in definitions if tool.derivation}
    assert derivations == {"delegate_task": "delegate", "fork_task": "fork", "agent_team": "team"}
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
    assert {"delegate_task", "child_agent_send", "child_agent_status", "wait_messages"} <= names
    # fork 出的是侧栏对话：只读 Task 不能 fork。
    assert "fork_task" not in names
    assert {"read_file", "execute_terminal", "browser_navigate"} <= names


def test_unknown_task_type_is_an_assembly_defect() -> None:
    with pytest.raises(KeyError):
        tools_for_task("mystery", [])


def test_team_node_tools_filter_by_rule_and_keep_unregistered_names() -> None:
    """节点工具按规则过滤、不代为追加；注册表里没有的名字原样保留，让节点建 Task 时如实报错。"""
    profile = SimpleNamespace(
        allowed_tools=["read_file", "agent_team", "terminal_start", "delegate_task", "ghost"]
    )
    kept = materialize_node_tools(profile)  # type: ignore[arg-type]
    assert kept == ["read_file", "delegate_task", "ghost"]
