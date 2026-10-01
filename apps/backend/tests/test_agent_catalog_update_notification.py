"""子 Agent 目录变更的延迟通知测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from app.core.agents.agent_profile import AgentProfileType
from app.task_runtime.agent_catalog_change import AgentCatalogChange
from app.task_runtime.broadcaster.agent_catalog_update_broadcaster import (
    broadcast_agent_catalog_change,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _FakeContextManager:
    """供目录通知过滤测试使用的最小 context manager 外壳。"""

    def __init__(self, agent_type: AgentProfileType, allowed_tools: list[str], root: str) -> None:
        self.agent_profile = SimpleNamespace(agent_type=agent_type, allowed_tools=allowed_tools)
        self.workspace_root = root


class _FakeRuntimeSpace:
    """不依赖 SQLite 的 runtime space 队列外壳。"""

    def __init__(self, manager: _FakeContextManager) -> None:
        self._manager = manager
        self.messages: list[SystemMessage] = []

    def get_context_manager(self) -> _FakeContextManager:
        return self._manager

    def defer_system_message(self, message: SystemMessage) -> None:
        self.messages.append(message)

    def take_deferred_system_messages(self) -> list[SystemMessage]:
        messages = self.messages
        self.messages = []
        return messages


@pytest.fixture(autouse=True)
def clear_runtime_spaces():
    """隔离每个用例的进程内 Task runtime space。"""

    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()


def _install_manager(task_id: int, manager: _FakeContextManager) -> None:
    """把测试 manager 安装到不依赖 SQLite 的 runtime space 外壳。"""

    space = _FakeRuntimeSpace(manager)
    task_runtime_spaces._spaces[task_id] = space


def test_agent_catalog_change_reaches_only_delegating_agents_in_scope() -> None:
    """目录变更只进入拥有委派工具且命中作用域的 Agent 队列。"""

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

    matching_messages = [
        task_runtime_spaces.get(task_id).take_deferred_system_messages()
        for task_id in (1, 2)
    ]
    assert all(len(messages) == 1 for messages in matching_messages)
    message = matching_messages[0][0]
    assert isinstance(message, SystemMessage)
    assert message.additional_kwargs["notice_type"] == "agent_catalog_change"
    assert "旧描述" in message.content
    assert "新描述" in message.content
    for task_id in (3, 4, 5):
        assert task_runtime_spaces.get(task_id).take_deferred_system_messages() == []


def test_system_agent_catalog_change_reaches_all_delegating_agents() -> None:
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
    assert len(task_runtime_spaces.get(10).take_deferred_system_messages()) == 1
    assert len(task_runtime_spaces.get(11).take_deferred_system_messages()) == 1
