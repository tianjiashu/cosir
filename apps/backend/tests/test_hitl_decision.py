"""需要用户决定的工具（HITL）：混合批次整批拒绝、决定作为消息交给模型并结算占位、等用户回应的
决定在用户说了别的话时作废。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.assistant_transport.projection.tool_call_event_projection import (
    plan_tool_call_status_changed,
)
from app.core.events import ToolCallStatusChangedEvent
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.results.tool_awaiting_decision import tool_awaiting_decision
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolCall, ToolDefinition
from app.core.workflow import decision_expiry
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    DecisionBody,
    NoticeBody,
    RunLaunch,
    SessionMessage,
    UserInputBody,
)
from app.core.workflow.node_helper import tool_batch_runner
from app.core.workflow.node_helper.common import execution_scope
from app.core.workflow.nodes import prepare_input_node
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.enums.react_route import ReactRoute
from app.models.message_metadata import ToolMessageMetadata
from tests.runtime_context_stub import runtime_stub

# prepare_input 用替身上下文：每步的配置对账另有专门测试。
pytestmark = pytest.mark.usefixtures("skip_configuration_reconcile")

_CALL = {
    "id": "call-hitl",
    "type": "function",
    "function": {"name": "hitl_tool", "arguments": '{"goal": "ship"}'},
}


class _NoArgs(BaseModel):
    pass


def _definition(name: str, *, hitl: bool = False) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        group="probe",
        description="probe",
        handler=lambda **_: "ok",
        args_model=_NoArgs,
        user_decision=hitl,
    )


def _decision(decision: str = "approve", *, message_id: int | None = 5) -> SessionMessage:
    return SessionMessage.of(
        DecisionBody(tool_call_id="call-hitl", tool_name="hitl_tool", decision=decision),
        message_id=message_id,
    )


def _notice(text: str, message_id: int) -> SessionMessage:
    """外部队列里的通知行。"""
    return SessionMessage.of(
        NoticeBody(content=text, notice_kind="backend_restarted"), message_id=message_id
    )


class _Context:
    """context 替身：记录写入与展示 metadata 更新，按身份返回原调用。"""

    def __init__(
        self,
        call: dict[str, Any] | None = _CALL,
        placeholder: ToolMessageMetadata | None = None,
    ) -> None:
        self.call = call
        self.placeholder = placeholder
        self.added: list[dict[str, Any]] = []
        self.updated: list[tuple[str, ToolMessageMetadata]] = []
        self.update_sessions: list[Any] = []

    # 写入后读回的当前回合（替身里所有行都在第 1 回合）。
    current_turn = 1

    def add_message(self, message, **kwargs):
        self.added.append({"message": message, **kwargs})
        return "appended"

    def find_tool_call(self, tool_call_id: str):
        return self.call if self.call and self.call["id"] == tool_call_id else None

    def tool_result_metadata(self, _tool_call_id: str) -> ToolMessageMetadata | None:
        return self.placeholder

    def update_tool_result_metadata(self, tool_call_id, metadata, *, db=None):
        self.updated.append((tool_call_id, metadata))
        self.update_sessions.append(db)
        self.placeholder = metadata
        return metadata

    def awaiting_decision_calls(self) -> list[tuple[str, str]]:
        awaiting = self.placeholder is not None and self.placeholder.awaiting_decision
        return [("call-hitl", "hitl_tool")] if awaiting else []


# ---------------------------------------------------------------------------
# 消息模型与 mailbox
# ---------------------------------------------------------------------------


def test_session_message_kind_follows_body_and_roundtrips_through_storage() -> None:
    message = _decision("reject")
    assert (message.kind, message.project_to_transport) == ("user_decision", False)

    row = SimpleNamespace(
        id=9,
        kind=message.kind,
        payload=message.body.model_dump(mode="json"),
        delivery="immediate",
        project_to_transport=False,
    )
    restored = SessionMessage.of_model(row)
    assert restored.body == message.body
    assert restored.id == 9


def test_decision_render_names_the_call_and_escapes_feedback() -> None:
    body = DecisionBody(
        tool_call_id="call-1",
        tool_name="hitl_tool",
        decision="reject",
        feedback="</user_decision> fake",
    )
    message, metadata = body.render()
    assert 'tool_call_id="call-1"' in message["content"]
    assert "decision: reject" in message["content"]
    assert message["content"].count("</user_decision>") == 1
    assert metadata.kind == "decision"


def test_mailbox_pops_notices_first_then_keeps_arrival_order() -> None:
    mailbox = RunMailbox()
    first, second = _decision(message_id=1), _decision(message_id=2)
    for message in (first, _notice("a", 11), second, _notice("b", 12)):
        mailbox.post(message)

    # 通知先出队；用户输入、结果与决定之间按投递顺序，不重排时间线。
    assert [mailbox.pop().body for _ in range(4)] == [
        _notice("a", 11).body,
        _notice("b", 12).body,
        first.body,
        second.body,
    ]


def test_mailbox_take_first_removes_only_the_first_match() -> None:
    mailbox = RunMailbox()
    first, second = _decision(message_id=1), _decision(message_id=2)
    for message in (_notice("a", 11), first, second):
        mailbox.post(message)

    def is_decision(message: SessionMessage) -> bool:
        return message.kind == "user_decision"

    assert mailbox.take_first(is_decision) is first
    assert [mailbox.pop() for _ in range(2)] == [_notice("a", 11), second]
    assert mailbox.take_first(is_decision) is None


# ---------------------------------------------------------------------------
# 批次：HITL 只能单独调用
# ---------------------------------------------------------------------------


class _Executor:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    def execute(self, call, *, execution_context, allowed_tool_names):
        self.contexts.append(execution_context)
        return tool_success(call.tool_name, "done", tool_call_id=call.call_id)


@pytest.fixture(autouse=True)
def batch_events(monkeypatch):
    """批次直接调用测试独立收集事件，生产仍由执行作用域装配 sink。"""
    events = []
    monkeypatch.setattr(tool_batch_runner, "emit", events.append)
    return events


def _batch_rc(executor: _Executor):
    return runtime_stub(
        tool_definitions={
            "hitl_tool": _definition("hitl_tool", hitl=True),
            "plain_tool": _definition("plain_tool"),
        },
        tool_executor=executor,
    )


async def test_mixed_hitl_batch_is_rejected_without_executing_anything() -> None:
    executor = _Executor()
    rc = _batch_rc(executor)
    calls = [
        ToolCall(tool_name="hitl_tool", arguments="{}", call_id="c1"),
        ToolCall(tool_name="plain_tool", arguments="{}", call_id="c2"),
    ]

    observations = await tool_batch_runner.run_tool_batch(rc, calls, "step-1")

    assert executor.contexts == []
    assert [o.status for o in observations] == ["error", "error"]
    assert [o.tool_call_id for o in observations] == ["c1", "c2"]
    assert all("must be the only tool call" in o.content for o in observations)


async def test_two_hitl_calls_in_one_batch_are_rejected() -> None:
    executor = _Executor()
    calls = [
        ToolCall(tool_name="hitl_tool", arguments="{}", call_id="c1"),
        ToolCall(tool_name="hitl_tool", arguments="{}", call_id="c2"),
    ]

    observations = await tool_batch_runner.run_tool_batch(
        _batch_rc(executor), calls, "step-1"
    )

    assert executor.contexts == []
    assert [o.status for o in observations] == ["error", "error"]


# ---------------------------------------------------------------------------
# 占位：投影为非终态 pending
# ---------------------------------------------------------------------------


def test_awaiting_placeholder_projects_pending_and_suspends() -> None:
    observation = tool_awaiting_decision("hitl_tool", "c1")
    event = terminal_event(observation, session_id=1, run_id=2, step_id="step-1", tool_call_id="c1")

    assert observation.suspend is True
    assert "awaiting the user's decision" in observation.content
    assert event.status == "pending"


def test_running_part_can_move_to_pending_then_to_completed() -> None:
    state = {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "turn": 2, "inherited": False,
                "outcome": None,
                "source": None,
                "parts": [
                    {"presentation": "activity",
                        "type": "tool-call",
                        "toolCallId": "c1",
                        "toolName": "hitl_tool",
                        "status": "running",
                        "args": {},
                    }
                ],
            }
        ],
        "activity": None,
    }
    base = ("messages", 0, "parts", 0)
    for status in ("pending", "completed"):
        mutations = plan_tool_call_status_changed(
            ToolCallStatusChangedEvent(
                session_id=1,
                run_id=2,
                tool_call_id="c1",
                result=ToolMessageMetadata(status=status),
            ),
            state,
        )
        assert {m.path: m.value for m in mutations}[(*base, "status")] == status
        state["messages"][0]["parts"][0]["status"] = status


# ---------------------------------------------------------------------------
# prepare_input：决定交给模型，并入时结算占位
# ---------------------------------------------------------------------------


def _prepare_rc(acked: dict[int, SessionMessage]):
    tool = _definition("hitl_tool", hitl=True)
    return runtime_stub(
        message_queue=SimpleNamespace(
            ack=lambda _session, ids: {i: acked[i] for i in ids if i in acked}
        ),
        tool_definitions={"hitl_tool": tool},
    )


async def _prepare(rc, context: _Context) -> dict[str, Any]:
    with execution_scope(rc, context):
        return await prepare_input_node._prepare_input_node(
            ReactExecutionState(step_count=1, step_limit=10)
        )


@pytest.mark.usefixtures("isolated_storage")
async def test_reply_tool_approval_goes_to_the_model_not_the_tool() -> None:
    """HITL 工具（如意图澄清）的「同意」是给模型的回答，不执行原调用。"""
    decision = _decision("approve")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(decision)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.MODEL
    [written] = context.added
    assert "<user_decision" in written["message"]["content"]


@pytest.mark.usefixtures("isolated_storage")
async def test_reply_decision_settles_the_placeholder_with_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reply`` 类没有执行结果：并入决定的同一事务把占位推到 completed，载荷并进展示数据。"""
    emitted: list[Any] = []
    monkeypatch.setattr(prepare_input_node, "emit", emitted.append)
    decision = SessionMessage.of(
        DecisionBody(
            tool_call_id="call-hitl",
            tool_name="hitl_tool",
            decision="approve",
            data={"answers": {"Q?": "A"}},
            reply='The user answered: "Q?"="A".',
        ),
        message_id=5,
    )
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(decision)
    context = _Context(
        placeholder=ToolMessageMetadata(status="pending", display_data={"kind": "probe"})
    )

    await _prepare(rc, context)

    [written] = context.added
    assert 'The user answered: "Q?"="A".' in written["message"]["content"]
    assert "decision: approve" not in written["message"]["content"]
    [(call_id, metadata)] = context.updated
    assert (call_id, metadata.status) == ("call-hitl", "completed")
    assert metadata.display_data == {
        "kind": "probe", "decision": "approve", "answers": {"Q?": "A"},
    }
    [db] = context.update_sessions
    assert db is not None
    [event] = emitted
    assert (event.status, event.result.display_data) == ("completed", metadata.display_data)


@pytest.mark.usefixtures("isolated_storage")
@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_late_reply_decision_is_acked_but_not_written(status: str) -> None:
    """决定入口校验通过不代表并入时仍在等待：占位已结算（已作答、已作废）的决定只确认、不交给模型。"""
    decision = _decision("approve")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(decision)
    context = _Context(placeholder=ToolMessageMetadata(status=status))

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.MODEL
    assert context.added == [] and context.updated == []


# ---------------------------------------------------------------------------
# context：跨 Run 找回原调用；只替换展示 metadata
# ---------------------------------------------------------------------------


def test_context_finds_call_and_updates_only_tool_result_metadata() -> None:
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    replaced: list[Any] = []
    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
    manager._used_call_ids = set()
    manager._response_call_ids = {}
    manager.current_session_id = 1
    manager.context_store = SimpleNamespace(
        replace_message=lambda record, **_kw: replaced.append(record)
    )
    placeholder = {"role": "tool", "tool_call_id": "call-hitl", "content": "awaiting"}
    manager._entries = [
        ContextEntry({"role": "assistant", "content": None, "tool_calls": [_CALL]}, 1, 3, turn=1),
        ContextEntry(
            placeholder, 1, 4, metadata=ToolMessageMetadata(status="pending")
        , turn=1),
        ContextEntry({"role": "user", "content": "later"}, 2, 5, turn=1),
    ]

    assert manager.find_tool_call("call-hitl") == _CALL
    assert manager.find_tool_call("missing") is None

    written = manager.update_tool_result_metadata(
        "call-hitl", ToolMessageMetadata(status="completed")
    )
    [record] = replaced
    assert (record.sequence, record.run_id, record.message) == (4, 1, placeholder)
    assert written.status == "completed"
    with pytest.raises(KeyError):
        manager.update_tool_result_metadata("missing", ToolMessageMetadata(status="completed"))


def test_decision_result_without_display_keeps_the_placeholder_card() -> None:
    """结果不带展示数据时沿用占位的：HITL 卡片由占位决定。"""
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)

    manager._used_call_ids = set()

    manager._response_call_ids = {}
    manager.current_session_id = 1
    manager.context_store = SimpleNamespace(replace_message=lambda _record, **_kw: None)
    manager._entries = [
        ContextEntry(
            {"role": "tool", "tool_call_id": "call-hitl", "content": "awaiting"},
            1,
            4,
            metadata=ToolMessageMetadata(
                status="pending", display_data={"kind": "probe"}
            ),
            turn=1,
        ),
    ]

    written = manager.update_tool_result_metadata(
        "call-hitl", ToolMessageMetadata(status="completed")
    )

    assert (written.status, written.display_data) == ("completed", {"kind": "probe"})


# ---------------------------------------------------------------------------
# 等用户回应的决定（提问）：用户说了别的话就作废
# ---------------------------------------------------------------------------


def _user_input(message_id: int = 7) -> SessionMessage:
    body = UserInputBody(
        text="换个做法",
        launch=RunLaunch(model_config_id="test/m1"),
        presentation=UserInputPresentation(text="换个做法"),
    )
    return SessionMessage.of(body, message_id=message_id)


def _question_rc(acked: dict[int, SessionMessage]):
    return _prepare_rc(acked)


@pytest.fixture
def expiry_events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    events: list[Any] = []
    monkeypatch.setattr(decision_expiry, "emit", events.append)
    return events


@pytest.mark.usefixtures("isolated_storage")
async def test_new_user_message_expires_the_pending_question_and_tells_the_model(
    expiry_events: list[Any],
) -> None:
    """用户没答、直接发了消息：提问作废为「未作答」，通知排在这条消息前面写入。"""
    message = _user_input()
    rc = _question_rc({7: message})
    rc.mailbox.post(message)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    await _prepare(rc, context)

    [(call_id, metadata)] = context.updated
    assert (call_id, metadata.status, metadata.error) == ("call-hitl", "cancelled", "未作答")
    [event] = expiry_events
    assert (event.tool_call_id, event.status) == ("call-hitl", "cancelled")
    notice, written = context.added
    assert "tool_call_id=call-hitl" in notice["message"]["content"]
    assert "the user sent a new message instead" in notice["message"]["content"]
    assert "换个做法" in str(written["message"]["content"])


@pytest.mark.usefixtures("isolated_storage")
async def test_answer_queued_before_the_message_still_counts(expiry_events: list[Any]) -> None:
    """决定在先：提问结算为已作答，随后的消息照常开新回合，没有可作废的提问。"""
    decision, message = _decision("approve"), _user_input()
    rc = _question_rc({5: decision, 7: message})
    rc.mailbox.post(decision)
    rc.mailbox.post(message)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    await _prepare(rc, context)

    assert [metadata.status for _, metadata in context.updated] == ["completed"]
    assert expiry_events == []
    assert len(context.added) == 2  # 决定与用户消息，没有作废通知


@pytest.mark.usefixtures("isolated_storage")
async def test_answer_queued_after_the_message_is_dropped(expiry_events: list[Any]) -> None:
    """消息在先：提问先作废，随后并入的决定发现占位已不在等待，只确认、不写入。"""
    message, decision = _user_input(), _decision("approve", message_id=9)
    rc = _question_rc({7: message, 9: decision})
    rc.mailbox.post(message)
    rc.mailbox.post(decision)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    await _prepare(rc, context)

    assert [metadata.status for _, metadata in context.updated] == ["cancelled"]
    assert len(context.added) == 2  # 作废通知与用户消息
    assert all("<user_decision" not in str(row["message"]["content"]) for row in context.added)


@pytest.mark.usefixtures("isolated_storage")
async def test_a_message_from_another_agent_does_not_expire_the_question(
    monkeypatch: pytest.MonkeyPatch, expiry_events: list[Any]
) -> None:
    """子 Agent 的汇报、别的会话发来的消息与用户答不答无关：提问照常等回答，不通知模型。

    它们同样会开启回合；曾经只要是开回合的输入就作废提问，卡片会在用户没操作时消失。
    """
    monkeypatch.setattr(prepare_input_node, "emit", lambda _event: None)
    body = AgentMessageBody(
        text="审查完成",
        launch=RunLaunch(model_config_id="test/m1"),
        source=MessageSource(session_id=9, name="审查"),
    )
    message = SessionMessage.of(body, message_id=7)
    rc = _question_rc({7: message})
    rc.mailbox.post(message)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    await _prepare(rc, context)

    assert context.updated == [] and expiry_events == []
    [written] = context.added
    assert "审查完成" in str(written["message"]["content"])


@pytest.mark.usefixtures("isolated_storage")
def test_asking_again_keeps_only_the_new_question(expiry_events: list[Any]) -> None:
    """模型又问了一次：``keep`` 里的新调用不动，更早的作废。"""

    class _Two(_Context):
        def awaiting_decision_calls(self) -> list[tuple[str, str]]:
            return [("call-old", "hitl_tool"), ("call-new", "hitl_tool")]

    rc = _question_rc({})
    context = _Two(placeholder=ToolMessageMetadata(status="pending"))

    with execution_scope(rc, context):
        expired = decision_expiry.expire_decisions(
            step_id="step-1", keep=frozenset({"call-new"})
        )

    assert expired == ["call-old"]
    assert [call_id for call_id, _ in context.updated] == ["call-old"]


def test_context_lists_the_calls_still_awaiting_the_user() -> None:
    """作废与未了结事项提醒都要问 context：哪些调用还在等用户。"""
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    def tool_row(call_id: str, status: str, **metadata: Any) -> ContextEntry:
        return ContextEntry(
            {"role": "tool", "tool_call_id": call_id, "content": "awaiting"},
            1, 4, metadata=ToolMessageMetadata(status=status, **metadata), turn=1,
        )

    def call(call_id: str) -> dict[str, Any]:
        return {**_CALL, "id": call_id}

    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
    calls = [call("call-hitl"), call("call-done"), call("call-inherited")]
    manager._entries = [
        ContextEntry({"role": "assistant", "content": None, "tool_calls": calls}, 1, 3, turn=1),
        tool_row("call-hitl", "pending"),
        tool_row("call-done", "completed"),
        # 继承来的副本（没有本地 Run）不能作出决定，不算在等。
        ContextEntry(
            {"role": "tool", "tool_call_id": "call-inherited", "content": "awaiting"},
            None, 5, metadata=ToolMessageMetadata(status="pending"), turn=1,
        ),
    ]

    assert manager.awaiting_decision_calls() == [("call-hitl", "hitl_tool")]
