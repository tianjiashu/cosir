"""系统提示词 diff 的进程内通知测试。"""

from __future__ import annotations

import weakref
from types import SimpleNamespace

import pytest
from langchain_core.messages import SystemMessage

from app.core.agents.agent_profile import AgentProfileType
from app.task_runtime.broadcaster.system_prompt_update_broadcaster import (
    broadcast_system_prompt_delta,
    build_system_prompt_delta,
)
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _FakeContextManager:
    """供广播过滤测试使用的可弱引用 context manager 外壳。"""

    def __init__(self, agent_type: AgentProfileType, workspace_root: str) -> None:
        self.agent_profile = SimpleNamespace(agent_type=agent_type)
        self.workspace_root = workspace_root


@pytest.fixture(autouse=True)
def clear_runtime_spaces():
    """隔离每个用例的进程内 Task runtime space。"""

    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()


def test_build_system_prompt_delta_only_contains_changed_lines() -> None:
    """相同正文不广播，变化正文只生成 unified diff。"""

    assert (
        build_system_prompt_delta(
            source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
            previous="保持旧规则\n",
            current="保持新规则\n",
        )
        is not None
    )
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="保持不变\n删除规则\n",
        current="保持不变\n新增规则\n",
    )
    assert delta is not None
    assert "保持不变" not in delta.diff
    assert "-删除规则" in delta.diff
    assert "+新增规则" in delta.diff
    assert (
        build_system_prompt_delta(
            source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
            previous="相同\n",
            current="相同\n",
        )
        is None
    )


def test_task_space_returns_delta_as_a_context_message() -> None:
    """系统提示词通知只消费一次，并交给模型节点沿既有路径写入 Task context。"""

    space = task_runtime_spaces.get_or_create(1)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧规则\n",
        current="新规则\n",
    )
    assert delta is not None

    space.defer_system_prompt_delta(delta)

    messages = space.take_deferred_system_messages()
    assert len(messages) == 1
    assert isinstance(messages[0], SystemMessage)
    assert "-旧规则" in messages[0].content
    assert "+新规则" in messages[0].content
    assert messages[0].additional_kwargs["notice_type"] == "system_prompt_delta"
    assert space.take_deferred_system_messages() == []


def test_main_agent_delta_only_reaches_main_agent_spaces() -> None:
    """主 Agent prompt 变更只通知 AgentProfileType.MAIN 的已物化 space。"""

    main_space = task_runtime_spaces.get_or_create(10)
    child_space = task_runtime_spaces.get_or_create(11)
    main_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/main")
    child_manager = _FakeContextManager(AgentProfileType.CHILD, "/workspace/main")
    main_space._context_manager = weakref.ref(main_manager)
    child_space._context_manager = weakref.ref(child_manager)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.MAIN_AGENT_PROMPT,
        previous="旧主 Agent 规则\n",
        current="新主 Agent 规则\n",
    )
    assert delta is not None

    assert broadcast_system_prompt_delta(delta) == 1

    assert len(main_space.take_deferred_system_messages()) == 1
    assert child_space.take_deferred_system_messages() == []


def test_global_delta_reaches_all_materialized_context_spaces() -> None:
    """系统级 AGENTS.md 变更通知所有已创建 context manager 的 space。"""

    first = task_runtime_spaces.get_or_create(20)
    second = task_runtime_spaces.get_or_create(21)
    first_manager = _FakeContextManager(AgentProfileType.CHILD, "/workspace/one")
    second_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/two")
    first._context_manager = weakref.ref(first_manager)
    second._context_manager = weakref.ref(second_manager)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧全局规则\n",
        current="新全局规则\n",
    )
    assert delta is not None

    assert broadcast_system_prompt_delta(delta) == 2
    assert len(first.take_deferred_system_messages()) == 1
    assert len(second.take_deferred_system_messages()) == 1
