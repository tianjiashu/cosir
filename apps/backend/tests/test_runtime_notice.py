"""驱动内通知的契约：驱动开始时推导、投进 mailbox（没有队列行、不落库），不唤醒，随 reset 丢弃。"""

from types import SimpleNamespace
from typing import cast

import pytest

from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    BackgroundResultBody,
    DecisionBody,
    NoticeBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.core.workflow.node_helper.common import execution_scope
from app.core.workflow.nodes import prepare_input_node
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import NoticeEntry
from app.models.session_kind import SessionType
from tests.runtime_context_stub import runtime_stub


def _space(mailbox: RunMailbox) -> SimpleNamespace:
    """Run 所属 Session 的运行时空间替身：``for_run`` 只从它取 Session / 工作区 / mailbox。"""
    return SimpleNamespace(
        session_tool_names=("read_file", "write_file"),
        session_id=1,
        current_task=SimpleNamespace(id=1, session_type=SessionType.USER, derivation_depth=0),
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
        "app.service.depends.get_conversation_event_projector",
        lambda: SimpleNamespace(process=print,
    ),
    )


def _for_run(monkeypatch, space, banned=None, *, run_id: int = 7, preceding=None):
    """装配一次驱动；``banned`` 给出时替换派生规则的禁用结果，``None`` 时走真实的 Session 类型规则。"""
    from app.core.workflow.execution_state import runtime_context as runtime_context_module

    _stub_for_run_deps(monkeypatch, agent=SimpleNamespace())
    monkeypatch.setattr(
        runtime_context_module.session_runtime_spaces, "get_or_create", lambda _id: space
    )
    if banned is not None:
        monkeypatch.setattr(
            runtime_context_module,
            "banned_derivation_tools",
            lambda _type, _depth: frozenset(banned),
        )
    return runtime_context_module.RuntimeContext.for_run(
        SimpleNamespace(id=run_id, session_id=1), preceding
    )


def _announced(run_id: int | None, *banned: str) -> tuple[int | None, NoticeEntry]:
    """上下文里最近一条准入通知：出自哪个 Run、当时禁用了什么。"""
    return run_id, NoticeEntry(
        notice_kind="tool_availability", content="...", banned_tools=sorted(banned)
    )


def _context(
    latest: tuple[int | None, NoticeEntry] | None = None,
    awaiting: tuple[tuple[str, str], ...] = (),
) -> SimpleNamespace:
    """上下文替身：``latest`` 是上下文里最近一条通知（按它自己的种类查得到），``awaiting`` 是仍在
    等用户决定的调用「id、工具名」。"""
    return SimpleNamespace(
        latest_notice=lambda kind: latest
        if latest is not None and latest[1].notice_kind == kind
        else None,
        awaiting_decision_calls=lambda: list(awaiting),
    )


@pytest.fixture(autouse=True)
def open_sources(monkeypatch):
    """未了结事项的另外两个事实来源：后台命令与派生 Session，默认都没有。"""
    from app.core.workflow import drive_notices as module

    sources = SimpleNamespace(commands=[], derived=[])
    monkeypatch.setattr(
        module, "background_commands", lambda: SimpleNamespace(pending=lambda _id: sources.commands)
    )
    monkeypatch.setattr(module, "open_derived_titles", lambda _id: sources.derived)
    return sources


def _notices(monkeypatch, space, banned=None, latest=None, *, run_id: int = 7) -> list[NoticeBody]:
    """装配一次驱动并推导通知；``latest`` 是上下文里最近一条准入通知（没有为 ``None``）。"""
    from app.core.workflow.drive_notices import drive_notices

    rc = _for_run(monkeypatch, space, banned, run_id=run_id)
    return [
        n for n in drive_notices(rc, _context(latest)) if n.notice_kind == "tool_availability"
    ]


@pytest.mark.parametrize("banned", [[], ["read_file"], ["read_file", "write_file"]])
def test_for_run_computes_admission_without_notifying(monkeypatch, banned) -> None:
    """装配只算准入边界，通知留给 ``begin_run`` 之后推导。"""
    mailbox = RunMailbox()
    rc = _for_run(monkeypatch, _space(mailbox), banned)

    assert rc.mailbox is mailbox
    assert rc.admitted_tools == {"read_file", "write_file"} - set(banned)
    assert rc.banned_tools == frozenset(banned)
    assert mailbox.take_notices() == []


def test_agent_fork_is_told_and_refused_fork_session_without_changing_its_schema(monkeypatch) -> None:
    """分叉的工具列表与来源相同，``fork_session`` 只在准入层被拒，并由工具准入通知提前告知。"""
    space = _space(RunMailbox())
    space.session_tool_names = ("read_file", "fork_session", "delegate_task")
    space.current_task = SimpleNamespace(id=1, session_type=SessionType.AGENT_FORK, derivation_depth=1)

    [notice] = _notices(monkeypatch, space)

    assert notice.banned_tools == ["fork_session"]
    # 禁用来源是 Session 类型规则：说清「这个 Session 不能用」，而不是「这次不能用」。
    assert "for this session: fork_session" in notice.content
    assert "round" not in notice.content
    # 可用工具不列举：它们已在 schema 与系统提示词的工具目录里，列出来只会被读成也被禁用。
    assert "read_file" not in notice.content and "delegate_task" not in notice.content


def test_derivation_rules_do_not_announce_tools_the_session_never_had(monkeypatch) -> None:
    """委派子 Session 的 schema 本来就没有 ``fork_session``，不能通知成「被禁用」。"""
    space = _space(RunMailbox())
    space.current_task = SimpleNamespace(id=1, session_type=SessionType.DELEGATED, derivation_depth=1)

    [notice] = _notices(monkeypatch, space)

    assert notice.banned_tools == []
    assert "all tools are available" in notice.content


def test_tool_availability_compares_with_what_the_context_last_said(monkeypatch) -> None:
    """基准取自上下文：全部可用且没变时不说；有变化给增量；新 Run 提醒一次仍在禁用的工具。"""
    space = _space(RunMailbox())
    assert _notices(monkeypatch, space, [], _announced(5)) == []

    [notice] = _notices(monkeypatch, space, ["write_file"], _announced(5))
    assert "newly disabled: write_file" in notice.content
    assert notice.banned_tools == ["write_file"]

    [notice] = _notices(monkeypatch, space, ["write_file"], _announced(5, "write_file"))
    assert "still disabled" in notice.content


def test_tool_availability_is_said_once_per_run(monkeypatch) -> None:
    """本 Run 已说过且没变（挂起后唤醒、恢复）就不再说；继承来的历史按更早的 Run 对待。"""
    space = _space(RunMailbox())

    assert _notices(monkeypatch, space, ["write_file"], _announced(7, "write_file")) == []
    [inherited] = _notices(monkeypatch, space, ["write_file"], _announced(None, "write_file"))
    assert "still disabled" in inherited.content


def test_locked_derivation_is_announced_per_session_and_never_re_enabled(monkeypatch) -> None:
    """Session 级禁用（派生封顶）配置不变时每个 Run 都成立：下一个 Run 只说 still disabled。"""
    space = _space(RunMailbox())
    space.session_tool_names = ("read_file", "fork_session")
    space.current_task = SimpleNamespace(id=1, session_type=SessionType.AGENT_FORK, derivation_depth=1)

    [first] = _notices(monkeypatch, space)
    assert "disabled (for this session: fork_session)" in first.content

    [second] = _notices(monkeypatch, space, None, _announced(6, "fork_session"))
    assert "still disabled (for this session: fork_session)" in second.content
    assert "re-enabled" not in second.content


@pytest.mark.parametrize(
    ("end_reason", "withdrawn", "told"),
    [
        ("user_cancelled", False, True),
        ("user_cancelled", True, False),
        ("parent_cancelled", False, False),
        ("completed", False, False),
    ],
)
def test_user_stopped_is_derived_from_how_the_preceding_run_ended(
    monkeypatch, end_reason, withdrawn, told
) -> None:
    """「用户已停止」只在上一次驱动以用户停止结束、且那一回合模型仍看得到时出现。"""
    from app.core.workflow import drive_notices as module

    monkeypatch.setattr(
        module,
        "ContextStore",
        lambda: SimpleNamespace(run_last_turn_withdrawn=lambda _session, _run: withdrawn),
    )
    preceding = SimpleNamespace(id=6, end_reason=end_reason)
    rc = _for_run(monkeypatch, _space(RunMailbox()), preceding=preceding)

    kinds = [
        n.notice_kind
        for n in module.drive_notices(rc, _context())
    ]

    assert ("user_cancelled" in kinds) is told


# ---------------------------------------------------------------------------
# 未了结事项：驱动开始时告诉模型它手上还有哪些事没了结
# ---------------------------------------------------------------------------


def _open(monkeypatch, context, mailbox: RunMailbox | None = None, *, run_id: int = 7):
    from app.core.workflow.drive_notices import drive_notices

    rc = _for_run(monkeypatch, _space(mailbox or RunMailbox()), run_id=run_id)
    return [n for n in drive_notices(rc, context) if n.notice_kind == "open_items"]


def _told(run_id: int | None, *items: str) -> tuple[int | None, NoticeEntry]:
    return run_id, NoticeEntry(notice_kind="open_items", content="...", open_items=sorted(items))


def _answer(call_id: str, message_id: int) -> SessionMessage:
    body = DecisionBody(tool_call_id=call_id, tool_name="ask_user", decision="approve")
    return SessionMessage.of(body, message_id=message_id)


def test_nothing_open_says_nothing(monkeypatch) -> None:
    assert _open(monkeypatch, _context()) == []


def test_open_items_lists_each_source_and_records_what_it_said(monkeypatch, open_sources) -> None:
    """被别的消息唤醒时，模型读到三类没了结的事各一行；清单随通知落库，作为下一次的比对基准。"""
    open_sources.commands, open_sources.derived = ["cargo check"], ["审查"]

    [notice] = _open(monkeypatch, _context(awaiting=(("call-ask", "ask_user"),)))

    assert notice.open_items == ["command:cargo check", "decision:call-ask", "session:审查"]
    assert "ask_user (tool_call_id=call-ask)" in notice.content
    assert "Do not repeat these calls" in notice.content
    assert "cargo check" in notice.content and "审查" in notice.content
    assert notice.wake_scope() == "none"


def test_open_items_is_said_once_per_run_until_the_list_changes(monkeypatch, open_sources) -> None:
    awaiting = (("call-ask", "ask_user"),)

    # 本 Run 说过、清单没变：不重复。
    assert _open(monkeypatch, _context(_told(7, "decision:call-ask"), awaiting)) == []
    # 上次是更早的 Run 说的：再提醒一次，防止长对话里遗忘。
    assert len(_open(monkeypatch, _context(_told(6, "decision:call-ask"), awaiting))) == 1
    # 清单变了（后台命令结束了，提问还在）：只说还剩下的。
    [notice] = _open(
        monkeypatch, _context(_told(7, "command:cargo check", "decision:call-ask"), awaiting)
    )
    assert notice.open_items == ["decision:call-ask"]
    assert "Background commands" not in notice.content


def test_decisions_this_drive_is_about_to_settle_are_not_open(monkeypatch, open_sources) -> None:
    """回答已在 mailbox 里的提问不算在等：否则模型先读到「还在等」再读到回答。"""
    from app.core.workflow import drive_notices as module

    registry = SimpleNamespace(
        get_tool_definition=lambda name: SimpleNamespace(
            decision_lifetime="until_user_input" if name == "ask_user" else "until_decided"
        )
    )
    monkeypatch.setattr("app.core.workflow.decision_expiry.get_tool_registry", lambda: registry)
    awaiting = (("call-ask", "ask_user"), ("call-hand", "browser_handoff"))

    answered = RunMailbox()
    answered.post(_answer("call-ask", 3))
    [notice] = _open(monkeypatch, _context(awaiting=awaiting), answered)
    assert notice.open_items == ["decision:call-hand"]

    # 用户发了新消息会让提问作废（另有「提问未被回答」的通知），交接照常等交还。
    new_turn = RunMailbox()
    new_turn.post(
        SessionMessage.of(
            UserInputBody(
                text="换个做法",
                launch=RunLaunch(model_config_id="test/m1"),
                presentation=UserInputPresentation(text="换个做法"),
            ),
            message_id=4,
        )
    )
    [notice] = _open(monkeypatch, _context(awaiting=awaiting), new_turn)
    assert notice.open_items == ["decision:call-hand"]
    assert module._OPEN_ITEMS_PREFIX in notice.content

    # 别的 Agent 发来的消息与用户答不答无关：提问照常在等，提醒里仍列着它。
    from_agent = RunMailbox()
    from_agent.post(
        SessionMessage.of(
            AgentMessageBody(
                text="审查完成",
                launch=RunLaunch(model_config_id="test/m1"),
                source=MessageSource(session_id=9, name="审查"),
            ),
            message_id=5,
        )
    )
    [notice] = _open(monkeypatch, _context(awaiting=awaiting), from_agent)
    assert notice.open_items == ["decision:call-ask", "decision:call-hand"]


def test_tool_availability_notice_rules() -> None:
    """规则是纯函数：按上次告知的集合与本 Run 的禁用集合取通知文本，无需装配上下文。"""
    from app.core.workflow.drive_notices import tool_availability_notice as notice

    def f(*names: str) -> frozenset[str]:
        return frozenset(names)

    # 未知：完整说明，禁用清单标明是 Session 级。
    assert notice(None, f()) == "Tool availability notice: all tools are available."
    assert notice(None, f("fork_session", "delegate_task")) == (
        "Tool availability notice: disabled (for this session: delegate_task, fork_session)."
    )
    # 未变化：全部可用时不通知；仍有禁用时重复提醒。
    assert notice(f(), f()) is None
    assert notice(f("a"), f("a")) == "Tool availability notice: still disabled (for this session: a)."
    # 变化：增量 + 当前状态。
    assert notice(f("a"), f("b")) == (
        "Tool availability notice: changed since last round "
        "(newly disabled: b; re-enabled: a); currently disabled (for this session: b)."
    )
    assert notice(f("a"), f()).endswith("(re-enabled: a); all tools are available now.")


def test_mailbox_dedupes_persisted_rows_within_one_execution() -> None:
    """同一外部队列行在一次执行内只收一次（派发方每次按外部队列全量投递）。"""
    mailbox = RunMailbox()
    persisted = SessionMessage.of(NoticeBody(content="broadcast", notice_kind="system_prompt_delta"), message_id=5)

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
    """通知一次取光（外部行与执行内通知都在内），其余消息不动；普通通知不算可唤醒消息。"""
    mailbox = RunMailbox()
    row = SessionMessage.of(
        NoticeBody(content="catalog changed", notice_kind="catalog_change"), message_id=5
    )
    result = SessionMessage.of(
        BackgroundResultBody(
            tool_call_id="call-bg", tool_name="execute_terminal", tool_result="{}", ended_at="t",
        ),
        message_id=6,
    )
    mailbox.post(row)
    mailbox.notify(NoticeBody(content="run notice", notice_kind="model_continuation"))
    assert not mailbox.has_wakeable(1)
    mailbox.post(result)
    assert mailbox.has_wakeable(1)

    notices = mailbox.take_notices()

    assert [(n.id, cast(NoticeBody, n.body).content) for n in notices] == [
        (5, "catalog changed"),
        (None, "run notice"),
    ]
    assert mailbox.pop() == result
    assert mailbox.isEmpty()


class _Context:
    """context 替身：只记录写入。"""

    def __init__(self) -> None:
        self.added: list[dict] = []

    # 写入后读回的当前回合（替身里所有行都在第 1 回合）。
    current_turn = 1

    def add_message(self, message, **kwargs):
        self.added.append({"message": message, **kwargs})


@pytest.mark.usefixtures("isolated_storage")
async def test_prepare_input_writes_notices_from_both_sources_as_one_message() -> None:
    """外部通知行与执行内通知合成一条 ``<system_notice>``，行与写入同一事务确认。

    已被别处确认的行跳过。
    """
    row = SessionMessage.of(
        NoticeBody(content="terminal ended", notice_kind="backend_restarted"), message_id=4
    )
    stale = SessionMessage.of(
        NoticeBody(content="already acked", notice_kind="backend_restarted"), message_id=9
    )
    acks: list[list[int]] = []

    def ack(db, ids):
        acks.append(ids)
        return {4: row} if 4 in ids else {}

    rc = runtime_stub(message_queue=SimpleNamespace(ack=ack))
    rc.mailbox.post(row)
    rc.mailbox.post(stale)
    rc.mailbox.notify(NoticeBody(content="run notice", notice_kind="model_continuation"))
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
    assert written["db"] is not None
    assert rc.mailbox.isEmpty()
