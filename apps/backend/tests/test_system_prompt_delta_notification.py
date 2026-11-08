"""系统提示词 diff 的通知广播测试：命中的 Task 经 ``task_runtime_spaces.enqueue`` 入外部队列。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfileType
from app.core.workflow.message_queue.task_message import NoticeBody, notices_to_context
from app.service.configuration.runtime_notices.system_prompt_delta import (
    SystemPromptDelta,
    SystemPromptDeltaSource,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class _TaskFacts:
    """过滤只读的 Task 事实：space 记下的 Agent 与工作区。"""

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
            get_task=lambda task_id: SimpleNamespace(id=task_id, workspace_id=1, tool_schemas=[])
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
    """记录广播经 ``enqueue`` 写入的通知，按 Task 归档。"""

    by_task: dict[int, list[NoticeBody]] = {}

    def enqueue(task_id: int, message, **_kw) -> int:
        by_task.setdefault(task_id, []).append(message.body)
        return 0

    monkeypatch.setattr(task_runtime_spaces, "enqueue", enqueue)
    return by_task


def test_delta_between_only_contains_changed_lines() -> None:
    """相同正文不广播，变化正文只生成 unified diff。"""

    assert (
        SystemPromptDelta.between(
            source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
            previous="保持旧规则\n",
            current="保持新规则\n",
        )
        is not None
    )
    delta = SystemPromptDelta.between(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="保持不变\n删除规则\n",
        current="保持不变\n新增规则\n",
    )
    assert delta is not None
    assert "保持不变" not in delta.diff
    assert "-删除规则" in delta.diff
    assert "+新增规则" in delta.diff
    assert (
        SystemPromptDelta.between(
            source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
            previous="相同\n",
            current="相同\n",
        )
        is None
    )


def test_delta_notice_becomes_one_tagged_user_message() -> None:
    """通知合并为一条带 ``<system_notice>`` 标签的 user 消息：只含正文，读法说明在系统提示词里。"""

    delta = SystemPromptDelta.between(
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
    assert content.startswith('<system_notice time="')
    assert content.count("<system_notice ") == 1
    assert content.endswith(f'">\n{notice.content}\n{notice.content}\n</system_notice>')
    assert "-旧规则" in notice.content
    assert "+新规则" in notice.content


def _materialize(task_id: int, facts: _TaskFacts):
    """物化真实 space，并记下它装配过的 Agent 与所在工作区（context manager 不必存活）。"""
    space = task_runtime_spaces.get_or_create(task_id)
    space.agent = facts.agent
    space.current_workspace = facts.workspace
    return space


def test_main_agent_delta_only_reaches_main_agent_spaces(posted) -> None:
    """主 Agent prompt 变更只通知 AgentProfileType.MAIN 的已物化 space。"""

    main_manager = _TaskFacts(AgentProfileType.MAIN, "/workspace/main")
    child_manager = _TaskFacts(AgentProfileType.CHILD, "/workspace/main")
    _materialize(10, main_manager)
    _materialize(11, child_manager)
    delta = SystemPromptDelta.between(
        source=SystemPromptDeltaSource.MAIN_AGENT_PROMPT,
        previous="旧主 Agent 规则\n",
        current="新主 Agent 规则\n",
    )
    assert delta is not None

    assert delta.broadcast() == 1

    assert list(posted) == [10]
    assert "+新主 Agent 规则" in posted[10][0].content


def test_global_delta_reaches_all_materialized_context_spaces(posted) -> None:
    """系统级 AGENTS.md 变更通知所有已创建 context manager 的 space，同一条通知不被改写。"""

    first_manager = _TaskFacts(AgentProfileType.CHILD, "/workspace/one")
    second_manager = _TaskFacts(AgentProfileType.MAIN, "/workspace/two")
    _materialize(20, first_manager)
    _materialize(21, second_manager)
    delta = SystemPromptDelta.between(
        source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
        previous="旧全局规则\n",
        current="新全局规则\n",
    )
    assert delta is not None

    assert delta.broadcast() == 2
    assert sorted(posted) == [20, 21]
    assert "+新全局规则" in posted[20][0].content


@pytest.mark.parametrize("remembered_profile", [True, False])
def test_filter_reads_the_space_agent_and_never_creates_a_manager(
    monkeypatch: pytest.MonkeyPatch, remembered_profile: bool
) -> None:
    """过滤只看 space 记下的 Agent：记得就通知（context manager 已被回收也一样），没装配过就跳过；
    两种情况都不得顺带创建 manager。

    回归一：过滤曾调用 ``get_context_manager()``，会为一次判断构造 manager（读库，新 Task 还会落库
    系统提示词），没有 profile 时直接抛 ``RuntimeError`` 让整次广播中断。回归二：过滤曾只认仍存活
    的弱引用 manager，空闲 Task 视 GC 时机漏收通知。
    """
    from app.core.context import runtime_message_context_manager as manager_module
    from app.service.configuration.runtime_notices.agent_catalog_change import (
        AgentCatalogChange,
    )

    def forbid_creation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("广播过滤不得创建 context manager")

    monkeypatch.setattr(manager_module, "RuntimeMessageContextManager", forbid_creation)
    space = task_runtime_spaces.get_or_create(40)
    if remembered_profile:
        space.agent = SimpleNamespace(
            agent_type=AgentProfileType.MAIN, allowed_tools=["delegate_task"]
        )
    delta = SystemPromptDelta.between(
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

    # 这个 Task 没有委派工具，目录变更本来就不发给它；这里只验证过滤不创建 manager。
    assert delta.broadcast() == (1 if remembered_profile else 0)
    assert change.broadcast() == 0
    assert space.live_context_manager() is None
