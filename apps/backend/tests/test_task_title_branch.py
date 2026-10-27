"""Task 标题支线：每个 Run 只起一次、成败不影响主线、写库后才投影。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.state.conversation_state_snapshot import (
    empty_snapshot,
    validate_snapshot,
)
from app.core.events import TaskTitleUpdatedEvent
from app.core.workflow.branch import task_title
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.inbox.task_message import NoticeBody, RunLaunch, TaskMessage, UserInputBody
from app.core.workflow.node_helper.common import execution_scope
from app.core.workflow.nodes import prepare_input_node
from app.service import depends as service_depends
from app.utils.datetime_utils import TASK_TITLE_LIMIT
from tests.runtime_context_stub import runtime_stub


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None
    )


class _Model:
    def __init__(self, content: str | Exception = '{"title": "重构登录模块"}') -> None:
        self.content = content
        self.requests: list[list[Any]] = []

    async def complete(self, messages, *, response_format):
        self.requests.append(list(messages))
        if isinstance(self.content, Exception):
            raise self.content
        return _completion(self.content)


def _titled_rc(model: _Model | None = None, *, task_type: str = "user") -> SimpleNamespace:
    events: list[Any] = []
    rc = runtime_stub(
        task=SimpleNamespace(id=1, task_type=task_type, title="你好"),
        model=model or _Model(),
        event_sink=events.append,
    )
    rc.events = events
    return rc


@pytest.fixture
def titles(monkeypatch) -> list[tuple[int, str]]:
    written: list[tuple[int, str]] = []
    service = SimpleNamespace(update_title=lambda task_id, title: written.append((task_id, title)))
    monkeypatch.setattr(service_depends, "get_task_service", lambda: service)
    return written


async def _run_branch(rc: SimpleNamespace) -> None:
    with execution_scope(rc, SimpleNamespace()):
        task_title.launch(rc, [{"role": "user", "content": "帮我重构登录"}])
    await rc.title_branch


# ── 触发条件 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("task_type", "requested", "expected"),
    [
        ("user", False, True),
        ("fork", False, True),
        ("user", True, False),
        ("delegate_task", False, False),
        ("agent_team_node", False, False),
    ],
)
def test_should_launch_once_per_run_and_only_for_sidebar_tasks(
    task_type: str, requested: bool, expected: bool
) -> None:
    rc = _titled_rc(task_type=task_type)
    state = ReactExecutionState(title_requested=requested)
    assert task_title.should_launch(rc, state) is expected


def test_title_requested_survives_new_drive() -> None:
    """唤醒与恢复都是新驱动：已起过标题的 Run 不能再起一次。"""
    state = ReactExecutionState(step_count=3, title_requested=True)
    assert state.for_new_drive(10).title_requested is True


class _Context:
    def __init__(self) -> None:
        self.loads = 0

    def load_message(self) -> list[dict[str, str]]:
        self.loads += 1
        return [{"role": "user", "content": "帮我重构登录"}]


def _user_input(message_id: int) -> TaskMessage:
    body = UserInputBody(
        display_text="帮我重构登录", input_text="帮我重构登录", launch=RunLaunch(model_config_id=1)
    )
    return TaskMessage.of(body, message_id=message_id)


async def _prepare(rc, context, state: ReactExecutionState) -> dict[str, Any]:
    with execution_scope(rc, context):
        return await prepare_input_node._prepare_input_node(state)


async def test_prepare_input_launches_on_user_input_and_marks_the_run(monkeypatch) -> None:
    monkeypatch.setattr(prepare_input_node, "_write_message", lambda _message: True)
    launched: list[Any] = []
    monkeypatch.setattr(task_title, "launch", lambda rc, messages: launched.append(messages))
    rc = _titled_rc()
    context = _Context()
    rc.mailbox.post(_user_input(7))

    patch = await _prepare(rc, context, ReactExecutionState(step_count=1, step_limit=10))

    assert len(launched) == 1
    assert patch["title_requested"] is True


async def test_prepare_input_skips_when_already_requested_or_no_user_input(monkeypatch) -> None:
    monkeypatch.setattr(prepare_input_node, "_write_message", lambda _message: True)
    launched: list[Any] = []
    monkeypatch.setattr(task_title, "launch", lambda rc, messages: launched.append(messages))
    context = _Context()

    rc = _titled_rc()
    rc.mailbox.post(_user_input(7))
    requested = ReactExecutionState(step_count=1, step_limit=10, title_requested=True)
    assert "title_requested" not in await _prepare(rc, context, requested)

    rc = _titled_rc()
    rc.mailbox.post(TaskMessage.of(NoticeBody(content="n", notice_kind="probe")))
    fresh = ReactExecutionState(step_count=1, step_limit=10)
    assert "title_requested" not in await _prepare(rc, context, fresh)

    assert launched == []
    assert context.loads == 0


# ── 生成与写入 ────────────────────────────────────────────────────────────


async def test_generated_title_is_written_then_projected(titles) -> None:
    model = _Model()
    rc = _titled_rc(model)

    await _run_branch(rc)

    assert titles == [(1, "重构登录模块")]
    assert [(type(e), e.title) for e in rc.events] == [(TaskTitleUpdatedEvent, "重构登录模块")]
    # 指令只追加在请求副本末尾，带上当前标题供模型保持稳定。
    assert '"你好"' in model.requests[0][-1]["content"]


@pytest.mark.parametrize(
    "content",
    ['{"title": "你好"}', "not json", '{"name": "x"}', '{"title": "  "}', RuntimeError("boom")],
)
async def test_unchanged_or_unusable_answers_keep_the_title(titles, content) -> None:
    rc = _titled_rc(_Model(content))

    await _run_branch(rc)

    assert titles == []
    assert rc.events == []


async def test_long_title_is_truncated_to_the_shared_limit(titles) -> None:
    rc = _titled_rc(_Model('{"title": "%s"}' % ("长" * 40)))

    await _run_branch(rc)

    assert len(titles[0][1]) == TASK_TITLE_LIMIT


async def test_deleted_task_drops_the_title_without_projecting(monkeypatch) -> None:
    def missing(_task_id: int, _title: str) -> None:
        raise KeyError(1)

    monkeypatch.setattr(
        service_depends, "get_task_service", lambda: SimpleNamespace(update_title=missing)
    )
    rc = _titled_rc()

    await _run_branch(rc)

    assert rc.events == []


# ── 收尾 ──────────────────────────────────────────────────────────────────


async def test_settle_waits_for_the_branch_when_not_cancelled() -> None:
    rc = _titled_rc()
    done = asyncio.Event()

    async def branch() -> None:
        await asyncio.sleep(0.05)
        done.set()

    rc.title_branch = asyncio.create_task(branch())
    await task_title.settle(rc)

    assert done.is_set()


async def test_settle_cancels_the_branch_on_stop_signal() -> None:
    rc = _titled_rc()
    rc.title_branch = asyncio.create_task(asyncio.sleep(60))
    rc.cancelled = True

    await asyncio.wait_for(task_title.settle(rc), timeout=1)

    assert rc.title_branch.cancelled()


async def test_settle_without_branch_is_a_no_op() -> None:
    await task_title.settle(_titled_rc())


# ── 投影 ──────────────────────────────────────────────────────────────────


def test_title_event_projects_to_snapshot_and_repeats_are_no_ops() -> None:
    state = empty_snapshot("你好")
    event = TaskTitleUpdatedEvent(task_id=1, run_id=2, title="重构登录模块")

    mutations = list(plan_event(event, state))
    assert [(m.kind, m.path, m.value) for m in mutations] == [("set", ("title",), "重构登录模块")]

    state["title"] = "重构登录模块"
    validate_snapshot(state)
    assert list(plan_event(event, state)) == []


def test_snapshot_requires_a_non_empty_title() -> None:
    with pytest.raises(ValueError):
        validate_snapshot({"title": " ", "messages": [], "activity": None})  # type: ignore[typeddict-item]


# ── 持久化 ────────────────────────────────────────────────────────────────


def test_update_title_keeps_sidebar_order_and_reports_missing_task(isolated_storage) -> None:
    """侧栏按 ``updated_at`` 排序：改标题不能让对话换位置。"""
    from pathlib import Path

    from app.service.depends import get_task_crud, get_workspace_crud

    workspace = get_workspace_crud().create("title-test", str(Path.cwd()))
    crud = get_task_crud()
    task = crud.create(workspace.id, "你好")

    crud.update_title(task.id, "重构登录模块")

    updated = crud.get(task.id)
    assert updated.title == "重构登录模块"
    assert updated.updated_at == task.updated_at
    with pytest.raises(KeyError):
        crud.update_title(task.id + 999, "x")
