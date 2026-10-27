"""子 Agent 目录变更的通知广播测试：命中的 Task 经 ``task_runtime_spaces.enqueue`` 入外部队列。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.core.workflow.inbox.task_message import NoticeBody
from app.task_runtime.agent_catalog_change import AgentCatalogChange
from app.task_runtime.broadcaster import runtime_notice_broadcaster
from app.task_runtime.broadcaster.agent_catalog_update_broadcaster import (
    broadcast_agent_catalog_change,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _FakeContextManager:
    """供目录通知过滤测试使用的最小 context manager 外壳。"""

    def __init__(self, agent_type: AgentProfileType, allowed_tools: list[str], root: str) -> None:
        self.agent = SimpleNamespace(agent_type=agent_type, allowed_tools=allowed_tools)
        self.workspace = SimpleNamespace(root_path=root)


class _FakeRuntimeSpace:
    """不依赖 SQLite 的 runtime space 外壳，只提供匹配所需的 Task 身份与 live manager。"""

    def __init__(self, task_id: int, manager: _FakeContextManager) -> None:
        self.task_id = task_id
        self._manager = manager

    def live_context_manager(self) -> _FakeContextManager:
        return self._manager


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

    monkeypatch.setattr(runtime_notice_broadcaster.task_runtime_spaces, "enqueue", enqueue)
    return by_task


def _install_manager(task_id: int, manager: _FakeContextManager) -> None:
    """把测试 manager 安装到不依赖 SQLite 的 runtime space 外壳。"""

    task_runtime_spaces._spaces[task_id] = _FakeRuntimeSpace(task_id, manager)


def test_agent_catalog_change_reaches_only_delegating_agents_in_scope(posted) -> None:
    """目录变更只投给拥有委派工具且命中作用域的 Agent 所在 Task。"""

    matching_main = _FakeContextManager(
        AgentProfileType.MAIN, ["delegate_task"], "/workspace/one"
    )
    matching_child = _FakeContextManager(
        AgentProfileType.CHILD, ["delegate_task"], "/workspace/one"
    )
    wrong_workspace = _FakeContextManager(
        AgentProfileType.MAIN, ["delegate_task"], "/workspace/two"
    )
    no_delegate = _FakeContextManager(AgentProfileType.CHILD, ["read_file"], "/workspace/one")
    hidden = _FakeContextManager(
        AgentProfileType.HIDDEN, ["delegate_task"], "/workspace/one"
    )
    managers = [matching_main, matching_child, wrong_workspace, no_delegate, hidden]
    for task_id, manager in enumerate(managers, start=1):
        _install_manager(task_id, manager)

    change = AgentCatalogChange(
        scope="/workspace/one",
        action="updated",
        agent_id="reviewer",
        previous_description="旧描述",
        current_description="新描述",
    )

    assert broadcast_agent_catalog_change(change) == 2

    assert sorted(posted) == [1, 2]
    assert all(len(posted[task_id]) == 1 for task_id in (1, 2))
    message = posted[1][0]
    assert isinstance(message, NoticeBody)
    assert message.notice_kind == "agent_catalog_change"
    assert "旧描述" in message.content
    assert "新描述" in message.content


def test_system_agent_catalog_change_reaches_all_delegating_agents(posted) -> None:
    """系统级目录变更跨 workspace 通知所有可委派 Agent。"""

    first = _FakeContextManager(AgentProfileType.MAIN, ["delegate_task"], "/workspace/one")
    second = _FakeContextManager(AgentProfileType.CHILD, ["delegate_task"], "/workspace/two")
    _install_manager(10, first)
    _install_manager(11, second)

    change = AgentCatalogChange(
        scope=None,
        action="created",
        agent_id="writer",
        previous_description=None,
        current_description="编写文档",
    )

    assert broadcast_agent_catalog_change(change) == 2
    assert {task_id: len(notices) for task_id, notices in posted.items()} == {10: 1, 11: 1}
