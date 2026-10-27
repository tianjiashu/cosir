"""Run 内通知的投递契约：只进本次执行的 mailbox，不落库、不需要确认。"""

from types import SimpleNamespace

import pytest

from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.inbox.task_message import NoticeBody, TaskMessage


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
async def test_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都向 mailbox 投递领域通知，不在装配方构造模型消息。"""
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    space = SimpleNamespace(
        task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}),
        announced_ban_tools=None,
    )
    monkeypatch.setattr(
        runtime_context_module.task_runtime_spaces, "get_or_create", lambda _id: space
    )
    agent = SimpleNamespace()
    mailbox = RunMailbox()
    result = runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, options=SimpleNamespace(ban_tools=banned)),
        SimpleNamespace(id=1),
        SimpleNamespace(id=13, root_path="/workspace"),
        agent,
        mailbox,
        event_sink=print,
    )
    assert result.agent is agent
    assert result.mailbox is mailbox
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    notice = mailbox.pop()
    assert mailbox.isEmpty()
    assert isinstance(notice.body, NoticeBody)
    assert notice.id is None
    assert notice.body.notice_kind == "tool_availability"
    assert space.announced_ban_tools == frozenset(banned)


def _for_run_with_space(monkeypatch, space, banned) -> RunMailbox:
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    monkeypatch.setattr(
        runtime_context_module.task_runtime_spaces, "get_or_create", lambda _id: space
    )
    mailbox = RunMailbox()
    runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, options=SimpleNamespace(ban_tools=banned)),
        SimpleNamespace(id=1),
        SimpleNamespace(id=13, root_path="/workspace"),
        SimpleNamespace(),
        mailbox,
        event_sink=print,
    )
    return mailbox


def test_tool_availability_skips_unchanged_all_available_and_reports_changes(monkeypatch) -> None:
    """已告知集合不变且全部可用时不投递；变化时投递增量并更新已告知集合。"""
    space = SimpleNamespace(
        task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}),
        announced_ban_tools=frozenset(),
    )
    assert _for_run_with_space(monkeypatch, space, []).isEmpty()

    mailbox = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "newly disabled: write_file" in mailbox.pop().body.content
    assert space.announced_ban_tools == frozenset({"write_file"})

    mailbox = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "still disabled" in mailbox.pop().body.content


def test_tool_availability_notice_rules() -> None:
    from app.core.workflow.execution_state.tool_availability_notice import (
        tool_availability_notice,
    )

    def f(*names: str) -> frozenset[str]:
        return frozenset(names)

    # 未知：总是完整说明，包括全部可用。
    assert "all tools are available" in tool_availability_notice(None, f(), f("a", "b"))
    assert "disabled for this round" in tool_availability_notice(None, f("b"), f("a"))
    # 未变化。
    assert tool_availability_notice(f(), f(), f("a", "b")) is None
    assert tool_availability_notice(f("b", "a"), f("a", "b"), f("c")) == (
        "Tool availability notice: still disabled for this round, "
        "calls to them will be rejected: a, b."
    )
    # 变化：增量 + 当前状态。
    assert tool_availability_notice(f("a"), f("b"), f("a", "c")) == (
        "Tool availability notice: changed since last round "
        "(newly disabled: b; re-enabled: a); currently disabled, calls to them will be rejected: b."
    )
    assert tool_availability_notice(f("a"), f(), f("a", "b")).endswith(
        "(re-enabled: a); all tools are available now."
    )
    assert tool_availability_notice(f(), f("a", "b"), f()).endswith("all tools are disabled now.")


def test_mailbox_dedupes_persisted_rows_but_keeps_every_run_notice() -> None:
    """同一外部队列行在一次执行内只收一次；Run 内通知没有行 id，逐条保留。"""
    mailbox = RunMailbox()
    persisted = TaskMessage.of(NoticeBody(content="broadcast", notice_kind="test"), message_id=5)
    transient = TaskMessage.of(NoticeBody(content="continue", notice_kind="test"))

    mailbox.post(persisted)
    mailbox.post(persisted)
    mailbox.post(transient)
    mailbox.post(transient)

    assert [mailbox.pop() for _ in range(3)] == [persisted, transient, transient]
    assert mailbox.isEmpty()
    # 取光后同一行再次投递仍被忽略：它已在本次执行中处理过；新执行开始时 reset 才重新接收。
    mailbox.post(persisted)
    assert mailbox.isEmpty()
    mailbox.reset()
    mailbox.post(persisted)
    assert mailbox.pop() == persisted
