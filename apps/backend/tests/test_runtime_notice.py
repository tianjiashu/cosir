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
from app.models.task_kind import TaskType
from tests.runtime_context_stub import runtime_stub


def _space(mailbox: RunMailbox, announced: frozenset[str] | None = None) -> SimpleNamespace:
    """Run 所属 Task 的运行时空间替身：``for_run`` 只从它取 Task / 工作区 / mailbox。"""
    return SimpleNamespace(
        task_tool_names=("read_file", "write_file"),
        announced_ban_tools=announced,
        task_id=1,
        current_task=SimpleNamespace(id=1, task_type=TaskType.USER, derivation_depth=0),
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
    assert result.admitted_tools == {"read_file", "write_file"} - set(banned)
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


def test_agent_fork_is_told_and_refused_fork_task_without_changing_its_schema(monkeypatch) -> None:
    """分叉的工具列表与来源相同，``fork_task`` 只在准入层被拒，并由工具准入通知提前告知。"""
    space = _space(RunMailbox())
    space.task_tool_names = ("read_file", "fork_task", "delegate_task")
    space.current_task = SimpleNamespace(id=1, task_type=TaskType.AGENT_FORK, derivation_depth=1)

    [notice] = _for_run_with_space(monkeypatch, space, [])

    assert space.announced_ban_tools == frozenset({"fork_task"})
    # 禁用来源是 Task 类型规则：说清「这个 Task 不能用」，而不是「本轮不能用」。
    assert "for this task: fork_task" in notice.content
    assert "round" not in notice.content
    # 可用工具不列举：它们已在 schema 与系统提示词的工具目录里，列出来只会被读成也被禁用。
    assert "read_file" not in notice.content and "delegate_task" not in notice.content


def test_derivation_rules_do_not_announce_tools_the_task_never_had(monkeypatch) -> None:
    """委派子 Task 的 schema 本来就没有 ``fork_task``，不能通知成「被禁用」。"""
    space = _space(RunMailbox())
    space.current_task = SimpleNamespace(id=1, task_type=TaskType.AGENT_TASK, derivation_depth=1)

    [notice] = _for_run_with_space(monkeypatch, space, [])

    assert space.announced_ban_tools == frozenset()
    assert "all tools are available" in notice.content


def test_tool_availability_skips_unchanged_all_available_and_reports_changes(monkeypatch) -> None:
    """已告知集合不变且全部可用时不产生通知；变化时给出增量并更新已告知集合。"""
    space = _space(RunMailbox(), announced=frozenset())
    assert _for_run_with_space(monkeypatch, space, []) == []

    [notice] = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "newly disabled: write_file" in notice.content
    assert space.announced_ban_tools == frozenset({"write_file"})

    [notice] = _for_run_with_space(monkeypatch, space, ["write_file"])
    assert "still disabled" in notice.content


def test_locked_derivation_is_announced_per_task_and_never_re_enabled(monkeypatch) -> None:
    """Task 级禁用（派生封顶）配置不变时每轮都成立：第二次只说 still disabled，不报 re-enabled。"""
    space = _space(RunMailbox())
    space.task_tool_names = ("read_file", "fork_task")
    space.current_task = SimpleNamespace(id=1, task_type=TaskType.AGENT_FORK, derivation_depth=1)

    [first] = _for_run_with_space(monkeypatch, space, [])
    assert "disabled (for this task: fork_task)" in first.content

    [second] = _for_run_with_space(monkeypatch, space, [])
    assert "still disabled (for this task: fork_task)" in second.content
    assert "re-enabled" not in second.content


def test_tool_groups_only_report_tools_the_task_has(monkeypatch) -> None:
    """用户关掉的组里若有该 Task 从未拥有的工具，通知不提它（模型手里没有的工具不算被禁）。"""
    space = _space(RunMailbox())
    space.task_tool_names = ("read_file", "list_directory")

    [notice] = _for_run_with_space(monkeypatch, space, ["read_file", "write_file"])

    assert "for this run: read_file" in notice.content
    assert "write_file" not in notice.content
    assert space.announced_ban_tools == frozenset({"read_file"})


def _notice(
    announced: frozenset[str] | None,
    task_banned: frozenset[str],
    run_banned: frozenset[str],
    *,
    all_banned: bool = False,
) -> str | None:
    """按本 Run 的准入边界取通知文本；规则是纯函数，无需装配上下文。"""
    from app.core.workflow.execution_state.runtime_context import RuntimeContext

    return RuntimeContext.tool_availability_notice(
        announced, task_banned, run_banned, all_banned=all_banned
    )


def test_tool_availability_notice_rules() -> None:
    def f(*names: str) -> frozenset[str]:
        return frozenset(names)

    # 未知：完整说明，且按来源分组（Task 级 / Run 级不混成一句）。
    assert _notice(None, f(), f()) == "Tool availability notice: all tools are available."
    assert _notice(None, f("fork_task"), f()) == (
        "Tool availability notice: disabled (for this task: fork_task)."
    )
    assert _notice(None, f("fork_task"), f("write_file")) == (
        "Tool availability notice: disabled "
        "(for this task: fork_task; for this run: write_file)."
    )
    # 全部工具都被禁时只给结论，不逐个列名。
    assert _notice(None, f("fork_task"), f("write_file"), all_banned=True) == (
        "Tool availability notice: all tools are disabled."
    )
    # 未变化：全部可用时不通知；仍有禁用时按来源重复提醒。
    assert _notice(f(), f(), f()) is None
    assert _notice(f("a", "b"), f("a"), f("b")) == (
        "Tool availability notice: still disabled (for this task: a; for this run: b)."
    )
    # 变化：增量（不带来源）+ 当前状态（按来源）。
    assert _notice(f("a"), f("b"), f()) == (
        "Tool availability notice: changed since last round "
        "(newly disabled: b; re-enabled: a); currently disabled (for this task: b)."
    )
    assert _notice(f("a"), f(), f()).endswith("(re-enabled: a); all tools are available now.")
    assert _notice(f(), f("a"), f("b"), all_banned=True).endswith(
        "(newly disabled: a, b); all tools are disabled now."
    )


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
    assert not mailbox.has_wakeable(1)
    mailbox.post(result)
    assert mailbox.has_wakeable(1)

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
