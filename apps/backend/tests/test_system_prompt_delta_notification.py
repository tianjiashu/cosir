"""系统提示词 diff 的通知广播测试：命中的 Task 经 ``TaskActors.post`` 入外部队列。"""

from __future__ import annotations

import weakref
from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.models.task_message import NoticeBody, notices_to_context
from app.task_runtime.broadcaster import runtime_notice_broadcaster
from app.task_runtime.broadcaster.system_prompt_update_broadcaster import (
    broadcast_system_prompt_delta,
    build_system_prompt_delta,
)
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _FakeContextManager:
    """供广播过滤测试使用的可弱引用 context manager 外壳。"""

    def __init__(self, agent_type: AgentProfileType, workspace_root: str) -> None:
        self.agent = SimpleNamespace(agent_type=agent_type)
        self.workspace = SimpleNamespace(root_path=workspace_root)


@pytest.fixture(autouse=True)
def clear_runtime_spaces(monkeypatch):
    """使用真实 runtime space 与广播，只替换无关的 Task/Workspace 存储读取。"""

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


@pytest.fixture(autouse=True)
def posted(monkeypatch) -> dict[int, list[NoticeBody]]:
    """记录广播交给 ``broadcast`` 的通知，按 Task 归档。"""

    by_task: dict[int, list[NoticeBody]] = {}

    def broadcast(task_ids: list[int], notice: NoticeBody) -> None:
        for task_id in task_ids:
            by_task.setdefault(task_id, []).append(notice)

    monkeypatch.setattr(
        runtime_notice_broadcaster,
        "get_task_actors",
        lambda: SimpleNamespace(broadcast=broadcast),
    )
    return by_task


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


def test_delta_notice_becomes_one_tagged_user_message() -> None:
    """通知合并为一条带 ``<system_notice>`` 标签的 user 消息：声明只加一次，正文原样拼在后面。"""

    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧规则\n",
        current="新规则\n",
    )
    assert delta is not None
    notice = delta.to_notice()

    message, metadata = notices_to_context([notice, notice])

    assert message["role"] == "user"
    assert metadata.kind == "notice" and metadata.presentation is None
    content = message["content"]
    assert isinstance(content, str)
    assert content.startswith("<system_notice>")
    assert content.count("<system_notice>") == 1
    assert content.endswith(f"\n{notice.content}\n{notice.content}\n</system_notice>")
    assert "-旧规则" in notice.content
    assert "+新规则" in notice.content


def _materialize(task_id: int, manager: _FakeContextManager):
    space = task_runtime_spaces.get_or_create(task_id)
    space._context_manager = weakref.ref(manager)
    return space


def test_main_agent_delta_only_reaches_main_agent_spaces(posted) -> None:
    """主 Agent prompt 变更只通知 AgentProfileType.MAIN 的已物化 space。"""

    main_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/main")
    child_manager = _FakeContextManager(AgentProfileType.CHILD, "/workspace/main")
    _materialize(10, main_manager)
    _materialize(11, child_manager)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.MAIN_AGENT_PROMPT,
        previous="旧主 Agent 规则\n",
        current="新主 Agent 规则\n",
    )
    assert delta is not None

    assert broadcast_system_prompt_delta(delta) == 1

    assert list(posted) == [10]
    assert "+新主 Agent 规则" in posted[10][0].content


def test_global_delta_reaches_all_materialized_context_spaces(posted) -> None:
    """系统级 AGENTS.md 变更通知所有已创建 context manager 的 space，同一条通知不被改写。"""

    first_manager = _FakeContextManager(AgentProfileType.CHILD, "/workspace/one")
    second_manager = _FakeContextManager(AgentProfileType.MAIN, "/workspace/two")
    _materialize(20, first_manager)
    _materialize(21, second_manager)
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧全局规则\n",
        current="新全局规则\n",
    )
    assert delta is not None

    assert broadcast_system_prompt_delta(delta) == 2
    assert sorted(posted) == [20, 21]
    assert "+新全局规则" in posted[20][0].content


@pytest.mark.parametrize("remembered_profile", [True, False])
def test_unmaterialized_context_space_is_skipped_without_creating_manager(
    monkeypatch: pytest.MonkeyPatch, remembered_profile: bool
) -> None:
    """未装配 context 的 space：两个广播都不通知，且过滤判断不得顺带创建 manager。

    回归：过滤曾调用 ``get_context_manager()``。space 记得 Agent profile 时它会为一次判断构造
    manager（读库，新 Task 还会落库系统提示词）；没有 profile 时直接抛 ``RuntimeError``，让整次
    广播中断。
    """
    from app.core.context import runtime_message_context_manager as manager_module
    from app.task_runtime.agent_catalog_change import AgentCatalogChange
    from app.task_runtime.broadcaster.agent_catalog_update_broadcaster import (
        broadcast_agent_catalog_change,
    )

    def forbid_creation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("广播过滤不得创建 context manager")

    monkeypatch.setattr(manager_module, "RuntimeMessageContextManager", forbid_creation)
    space = task_runtime_spaces.get_or_create(40)
    if remembered_profile:
        space.agent = SimpleNamespace(
            agent_type=AgentProfileType.MAIN, allowed_tools=["delegate_task"]
        )
    delta = build_system_prompt_delta(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧全局规则\n",
        current="新全局规则\n",
    )
    assert delta is not None
    change = AgentCatalogChange(
        scope=None,
        action="created",
        agent_id="writer",
        previous_description=None,
        current_description="编写文档",
    )

    assert broadcast_system_prompt_delta(delta) == 0
    assert broadcast_agent_catalog_change(change) == 0
    assert space.live_context_manager() is None
