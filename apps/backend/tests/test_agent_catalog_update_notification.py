"""子 Agent 目录变更的通知广播测试：命中的 Task 经 ``task_runtime_spaces.enqueue`` 入外部队列。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.core.workflow.message_queue.task_message import NoticeBody
from app.service.configuration.runtime_notices.agent_catalog_change import AgentCatalogChange
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _TaskFacts:
    """过滤只读的 Task 事实：space 记下的 Agent、固化的工具名与工作区。"""

    def __init__(self, agent_type: AgentProfileType, tool_names: list[str], root: str) -> None:
        self.agent = SimpleNamespace(agent_type=agent_type)
        self.tool_names = tuple(tool_names)
        self.workspace = SimpleNamespace(root_path=root)


class _FakeRuntimeSpace:
    """不依赖 SQLite 的 runtime space 外壳，只提供匹配所需的 Task 身份、Agent、工具名与工作区。"""

    def __init__(self, task_id: int, facts: _TaskFacts) -> None:
        self.task_id = task_id
        self.agent = facts.agent
        self.task_tool_names = facts.tool_names
        self.current_workspace = facts.workspace


@pytest.fixture(autouse=True)
def clear_runtime_spaces():
    """隔离每个用例的进程内 Task runtime space。"""

    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()


@pytest.fixture(autouse=True)
def posted(monkeypatch) -> dict[int, list[NoticeBody]]:
    """记录广播经 ``enqueue`` 写入的通知，按 Task 归档。"""

    by_task: dict[int, list[NoticeBody]] = {}

    def enqueue(task_id: int, message, **_kw) -> int:
        by_task.setdefault(task_id, []).append(message.body)
        return 0

    monkeypatch.setattr(task_runtime_spaces, "enqueue", enqueue)
    return by_task


def _install_space(task_id: int, facts: _TaskFacts) -> None:
    """把测试用 Task 事实安装成不依赖 SQLite 的 runtime space 外壳。"""

    task_runtime_spaces._spaces[task_id] = _FakeRuntimeSpace(task_id, facts)


def test_agent_catalog_change_reaches_only_delegating_agents_in_scope(posted) -> None:
    """目录变更只投给拥有委派工具且命中作用域的 Agent 所在 Task。"""

    matching_main = _TaskFacts(
        AgentProfileType.MAIN, ["delegate_task"], "/workspace/one"
    )
    matching_child = _TaskFacts(
        AgentProfileType.CHILD, ["delegate_task"], "/workspace/one"
    )
    wrong_workspace = _TaskFacts(
        AgentProfileType.MAIN, ["delegate_task"], "/workspace/two"
    )
    no_delegate = _TaskFacts(AgentProfileType.CHILD, ["read_file"], "/workspace/one")
    hidden = _TaskFacts(
        AgentProfileType.HIDDEN, ["delegate_task"], "/workspace/one"
    )
    managers = [matching_main, matching_child, wrong_workspace, no_delegate, hidden]
    for task_id, manager in enumerate(managers, start=1):
        _install_space(task_id, manager)

    change = AgentCatalogChange(
        scope="/workspace/one",
        action="updated",
        agent_id="reviewer",
        previous_description="旧描述",
        current_description="新描述",
    )

    assert change.broadcast() == 2

    assert sorted(posted) == [1, 2]
    assert all(len(posted[task_id]) == 1 for task_id in (1, 2))
    message = posted[1][0]
    assert isinstance(message, NoticeBody)
    assert message.notice_kind == "agent_catalog_change"
    assert "旧描述" in message.content
    assert "新描述" in message.content


def test_system_agent_catalog_change_reaches_all_delegating_agents(posted) -> None:
    """系统级目录变更跨 workspace 通知所有可委派 Agent。"""

    first = _TaskFacts(AgentProfileType.MAIN, ["delegate_task"], "/workspace/one")
    second = _TaskFacts(AgentProfileType.CHILD, ["delegate_task"], "/workspace/two")
    _install_space(10, first)
    _install_space(11, second)

    change = AgentCatalogChange(
        scope=None,
        action="created",
        agent_id="writer",
        previous_description=None,
        current_description="编写文档",
    )

    assert change.broadcast() == 2
    assert {task_id: len(notices) for task_id, notices in posted.items()} == {10: 1, 11: 1}
