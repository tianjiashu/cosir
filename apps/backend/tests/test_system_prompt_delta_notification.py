"""系统提示词 diff 的进程内通知测试。"""

from __future__ import annotations

import weakref
from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.task_runtime.broadcaster.system_prompt_update_broadcaster import (
    broadcast_system_prompt_delta,
    build_system_prompt_delta,
)
from app.models.runtime_notice import RuntimeNotice
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _FakeContextManager:
    """供广播过滤测试使用的可弱引用 context manager 外壳。"""

    def __init__(self, agent_type: AgentProfileType, workspace_root: str) -> None:
        self.agent_profile = SimpleNamespace(agent_type=agent_type)
        self.workspace_root = workspace_root


@pytest.fixture(autouse=True)
def clear_runtime_spaces(monkeypatch):
    """使用真实队列与广播，只替换无关的 Task/Workspace 存储读取。"""

    import app.task_runtime.task_runtime_space as space_module

    monkeypatch.setattr(
        space_module,
        "get_task_service",
        lambda: SimpleNamespace(
            get_task=lambda task_id: SimpleNamespace(id=task_id, workspace_id=1)
        ),
    )
    monkeypatch.setattr(
        space_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda workspace_id: SimpleNamespace(id=workspace_id)
        ),
    )
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


def test_task_space_returns_delta_as_a_runtime_notice() -> None:
    """系统提示词通知只消费一次，并交给模型节点沿既有路径写入 Task context。"""

    space = task_runtime_spaces.get_or_create(1)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧规则\n",
        current="新规则\n",
    )
    assert delta is not None

    space.defer_runtime_notice(delta.to_notice())

    messages = space.take_runtime_notices()
    assert len(messages) == 1
    assert isinstance(messages[0], RuntimeNotice)
    assert "-旧规则" in messages[0].content
    assert "+新规则" in messages[0].content
    assert messages[0].kind == "system_prompt_delta"
    assert space.take_runtime_notices() == []


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

    assert len(main_space.take_runtime_notices()) == 1
    assert child_space.take_runtime_notices() == []


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
    assert len(first.take_runtime_notices()) == 1
    assert len(second.take_runtime_notices()) == 1


def test_broadcast_notice_is_unchanged_across_tasks_and_message_conversion() -> None:
    """同一通知广播后，每个 Task 转换出的消息互不影响，也不累加前缀。"""
    first = task_runtime_spaces.get_or_create(30)
    second = task_runtime_spaces.get_or_create(31)
    first_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/one")
    second_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/two")
    first._context_manager = weakref.ref(first_manager)
    second._context_manager = weakref.ref(second_manager)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧规则\n",
        current="新规则\n",
    )
    assert delta is not None
    assert broadcast_system_prompt_delta(delta) == 2
    first_notice = first.take_runtime_notices()[0]
    second_notice = second.take_runtime_notices()[0]
    assert first_notice is second_notice
    assert first_notice == delta.to_notice()
    first_message = first_notice.build_runtime_notice_message()
    second_message = second_notice.build_runtime_notice_message()
    assert first_message.content == second_message.content == "system_hint:" + first_notice.content
    first_message.content = "changed"
    first_message.additional_kwargs["source"] = "changed"
    assert second_message.content == "system_hint:" + first_notice.content
    assert second_message.additional_kwargs["source"] == first_notice.metadata["source"]
    assert first_notice == delta.to_notice()
