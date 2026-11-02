"""执行内通知的契约：装配时投进 mailbox（没有队列行、不落库），不唤醒，随 reset 丢弃。"""

from types import SimpleNamespace
from typing import cast

import pytest

from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.task_message import (
    BackgroundResultBody,
    NoticeBody,
    TaskMessage,
)
from app.core.workflow.node_helper.common import execution_scope
from app.core.workflow.nodes import prepare_input_node
from tests.runtime_context_stub import runtime_stub


def _space(mailbox: RunMailbox, announced: frozenset[str] | None = None) -> SimpleNamespace:
    """Run 所属 Task 的运行时空间替身：``for_run`` 只从它取 Task / 工作区 / mailbox。"""
    return SimpleNamespace(
        task_tool_schemas=({"name": "read_file"}, {"name": "write_file"}),
        announced_ban_tools=announced,
        task_id=1,
        current_task=SimpleNamespace(id=1),
        current_workspace=SimpleNamespace(id=13, root_path="/workspace"),
        mailbox=mailbox,
    )


def _stub_for_run_deps(monkeypatch, agent) -> None:
    """``for_run`` 只收 Run：Agent 配置与事件回调由它内部解析，这里换成最小替身。"""
    from app.core.agents import agent_runtime_config as agent_module
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    monkeypatch.setattr(
        agent_module.AgentRuntimeConfig, "for_run", classmethod(lambda _cls, _run: agent)
    )
    monkeypatch.setattr(
        runtime_context_module,
        "get_conversation_event_projector",
        lambda: SimpleNamespace(process=print),
    )
    # 默认不需要告知旧终端失效；该通知的投递另有用例覆盖。
    monkeypatch.setattr(
        runtime_context_module.restart_settlement, "first_drive_notice", lambda _space: None
    )
    # 测试里每个「组」只含同名工具，断言仍按工具名写。
    monkeypatch.setattr(
        runtime_context_module,
        "get_tool_registry",
        lambda: SimpleNamespace(tool_groups_to_tool_names=lambda groups: list(groups)),
    )


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
async def test_tool_availability_is_a_run_scoped_notice(monkeypatch, banned) -> None:
    """工具准入的三条分支都产生领域通知，投进 mailbox，不在装配方构造模型消息。"""
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    mailbox = RunMailbox()
    space = _space(mailbox)
    agent = SimpleNamespace()
    _stub_for_run_deps(monkeypatch, agent)
    monkeypatch.setattr(
        runtime_context_module.task_runtime_spaces, "get_or_create", lambda _id: space
    )
    result = runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, task_id=1, options=SimpleNamespace(disabled_tool_groups=banned))
    )
    assert result.agent is agent
    assert result.mailbox is mailbox
    assert result.allows_tools == {"read_file", "write_file"} - set(banned)
    [entry] = mailbox.take_notices()
    assert entry.id is None
    assert isinstance(entry.body, NoticeBody)
    assert entry.body.notice_kind == "tool_availability"
    assert space.announced_ban_tools == frozenset(banned)


def _for_run_with_space(monkeypatch, space, banned) -> list[NoticeBody]:
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    _stub_for_run_deps(monkeypatch, agent=SimpleNamespace())
    monkeypatch.setattr(
        runtime_context_module.task_runtime_spaces, "get_or_create", lambda _id: space
    )
    runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, task_id=1, options=SimpleNamespace(disabled_tool_groups=banned))
    )
    return [cast(NoticeBody, entry.body) for entry in space.mailbox.take_notices()]


def test_tool_availability_skips_unchanged_all_available_and_reports_changes(monkeypatch) -> None:
    """已告知集合不变且全部可用时不产生通知；变化时给出增量并更新已告知集合。"""
    space = _space(RunMailbox(), announced=frozenset())
    assert _for_run_with_space(monkeypatch, space, []) == []

    [notice] = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "newly disabled: write_file" in notice.content
    assert space.announced_ban_tools == frozenset({"write_file"})

    [notice] = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "still disabled" in notice.content


def test_terminals_invalidated_notice_is_posted_and_failure_does_not_block(monkeypatch) -> None:
    """首次驱动的旧终端失效通知随装配投进 mailbox；判定失败只记录，装配照常完成。"""
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    terminals = NoticeBody(content="terminals gone", notice_kind="terminals_invalidated")
    space = _space(RunMailbox(), announced=frozenset())
    _for_run_with_space(monkeypatch, space, [])
    monkeypatch.setattr(
        runtime_context_module.restart_settlement, "first_drive_notice", lambda _space: terminals
    )
    runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, task_id=1, options=SimpleNamespace(disabled_tool_groups=[]))
    )
    assert [entry.body for entry in space.mailbox.take_notices()] == [terminals]

    def broken(_space):
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime_context_module.restart_settlement, "first_drive_notice", broken)
    runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=7, task_id=1, options=SimpleNamespace(disabled_tool_groups=[]))
    )
    assert space.mailbox.isEmpty()


def _notice(allowed: frozenset[str], announced: frozenset[str] | None, banned: frozenset[str]):
    """按本 Run 的准入边界取通知文本；规则是纯函数，无需装配上下文。"""
    from app.core.workflow.execution_state.runtime_context import RuntimeContext

    return RuntimeContext.tool_availability_notice(announced, banned, allowed)


def test_tool_availability_notice_rules() -> None:
    def f(*names: str) -> frozenset[str]:
        return frozenset(names)

    # 未知：总是完整说明，包括全部可用。
    assert "all tools are available" in _notice(f("a", "b"), None, f())
    assert "disabled for this round" in _notice(f("a"), None, f("b"))
    # 未变化。
    assert _notice(f("a", "b"), f(), f()) is None
    assert _notice(f("c"), f("b", "a"), f("a", "b")) == (
        "Tool availability notice: still disabled for this round, "
        "calls to them will be rejected: a, b."
    )
    # 变化：增量 + 当前状态。
    assert _notice(f("a", "c"), f("a"), f("b")) == (
        "Tool availability notice: changed since last round "
        "(newly disabled: b; re-enabled: a); currently disabled, calls to them will be rejected: b."
    )
    assert _notice(f("a", "b"), f("a"), f()).endswith(
        "(re-enabled: a); all tools are available now."
    )
    assert _notice(f(), f(), f("a", "b")).endswith("all tools are disabled now.")


def test_mailbox_dedupes_persisted_rows_within_one_execution() -> None:
    """同一外部队列行在一次执行内只收一次（派发方每次按外部队列全量投递）。"""
    mailbox = RunMailbox()
    persisted = TaskMessage.of(NoticeBody(content="broadcast", notice_kind="test"), message_id=5)

    mailbox.post(persisted)
    mailbox.post(persisted)

    assert mailbox.pop() == persisted
    assert mailbox.isEmpty()
    # 取光后同一行再次投递仍被忽略：它已在本次执行中处理过；新执行开始时 reset 才重新接收。
    mailbox.post(persisted)
    assert mailbox.isEmpty()
    mailbox.reset()
    mailbox.post(persisted)
    assert mailbox.pop() == persisted


def test_mailbox_takes_notices_from_both_sources_in_delivery_order() -> None:
    """通知一次取光（外部行与执行内通知都在内），其余消息不动；通知不算可唤醒消息。"""
    mailbox = RunMailbox()
    row = TaskMessage.of(NoticeBody(content="terminal ended", notice_kind="test"), message_id=5)
    result = TaskMessage.of(
        BackgroundResultBody(tool_name="child_agent", tool_result="{}", ended_at="t"),
        message_id=6,
    )
    mailbox.post(row)
    mailbox.notify(NoticeBody(content="run notice", notice_kind="test"))
    assert not mailbox.has_wakeable()
    mailbox.post(result)
    assert mailbox.has_wakeable()

    notices = mailbox.take_notices()

    assert [(n.id, cast(NoticeBody, n.body).content) for n in notices] == [
        (5, "terminal ended"),
        (None, "run notice"),
    ]
    assert mailbox.pop() == result
    assert mailbox.isEmpty()


class _Context:
    """context 替身：只记录写入。"""

    def __init__(self) -> None:
        self.added: list[dict] = []

    def add_message(self, message, **kwargs):
        self.added.append({"message": message, **kwargs})


@pytest.mark.usefixtures("isolated_storage")
async def test_prepare_input_writes_notices_from_both_sources_as_one_message() -> None:
    """外部通知行与执行内通知合成一条 ``<system_notice>``，行与写入同一事务确认。

    已被别处确认的行跳过。
    """
    row = TaskMessage.of(NoticeBody(content="terminal ended", notice_kind="probe"), message_id=4)
    stale = TaskMessage.of(NoticeBody(content="already acked", notice_kind="probe"), message_id=9)
    acks: list[list[int]] = []

    def ack(session, ids):
        acks.append(ids)
        return {4: row} if 4 in ids else {}

    rc = runtime_stub(message_queue=SimpleNamespace(ack=ack))
    rc.mailbox.post(row)
    rc.mailbox.post(stale)
    rc.mailbox.notify(NoticeBody(content="run notice", notice_kind="probe"))
    context = _Context()

    with execution_scope(rc, context):
        await prepare_input_node._prepare_input_node(
            ReactExecutionState(step_count=1, step_limit=10)
        )

    assert acks == [[4, 9]]
    [written] = context.added
    content = written["message"]["content"]
    assert content.count("<system_notice") == 1
    assert "terminal ended" in content and "run notice" in content
    assert "already acked" not in content
    assert written["session"] is not None
    assert rc.mailbox.isEmpty()
