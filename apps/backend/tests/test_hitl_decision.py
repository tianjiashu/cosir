"""需要用户决定的工具（HITL）：混合批次整批拒绝、决定优先且每轮一条、批准绕过模型直接执行、
结果追加为消息且占位正文不变。"""

from __future__ import annotations

import dataclasses
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
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.message_queue.task_message import (
    DecisionBody,
    NoticeBody,
    TaskMessage,
)
from app.core.workflow.node_helper import tool_batch_runner
from app.core.workflow.node_helper.common import execution_scope
from app.core.workflow.node_helper.tool_observation_projection import (
    settle_decision_observation,
)
from app.core.workflow.nodes import prepare_input_node
from app.models.enums.react_route import ReactRoute
from app.models.message_metadata import ToolMessageMetadata
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.tool_batch import ToolBatch
from tests.runtime_context_stub import runtime_stub

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
        user_decision="execute" if hitl else None,
    )


def _decision(decision: str = "approve", *, message_id: int | None = 5) -> TaskMessage:
    return TaskMessage.of(
        DecisionBody(tool_call_id="call-hitl", tool_name="hitl_tool", decision=decision),
        message_id=message_id,
    )


def _notice(text: str) -> TaskMessage:
    return TaskMessage.of(NoticeBody(content=text, notice_kind="probe"))


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

    def add_message(self, message, **kwargs):
        self.added.append({"message": message, **kwargs})
        return "appended"

    def find_tool_call(self, tool_call_id: str):
        return self.call if self.call and self.call["id"] == tool_call_id else None

    def tool_result_metadata(self, _tool_call_id: str) -> ToolMessageMetadata | None:
        return self.placeholder

    def update_tool_result_metadata(self, tool_call_id, metadata, *, session=None):
        self.updated.append((tool_call_id, metadata))
        self.update_sessions.append(session)
        self.placeholder = metadata
        return metadata


# ---------------------------------------------------------------------------
# 消息模型与 mailbox
# ---------------------------------------------------------------------------


def test_task_message_kind_follows_body_and_roundtrips_through_storage() -> None:
    message = _decision("reject")
    assert (message.kind, message.project_to_transport) == ("user_decision", False)

    row = SimpleNamespace(
        id=9,
        kind=message.kind,
        payload=message.body.model_dump(mode="json"),
        delivery="immediate",
        project_to_transport=False,
    )
    restored = TaskMessage.of_model(row)
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
    for message in (first, _notice("a"), second, _notice("b")):
        mailbox.post(message)

    # 通知先出队；用户输入、结果与决定之间按投递顺序，不重排时间线。
    assert [mailbox.pop().body for _ in range(4)] == [
        _notice("a").body,
        _notice("b").body,
        first.body,
        second.body,
    ]


def test_mailbox_take_first_removes_only_the_first_match() -> None:
    mailbox = RunMailbox()
    first, second = _decision(message_id=1), _decision(message_id=2)
    for message in (_notice("a"), first, second):
        mailbox.post(message)

    def is_decision(message: TaskMessage) -> bool:
        return message.kind == "user_decision"

    assert mailbox.take_first(is_decision) is first
    assert [mailbox.pop() for _ in range(2)] == [_notice("a"), second]
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

    observations = await tool_batch_runner.run_tool_batch(rc, calls, "step-1", ToolBatch())

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
        _batch_rc(executor), calls, "step-1", ToolBatch()
    )

    assert executor.contexts == []
    assert [o.status for o in observations] == ["error", "error"]


async def test_single_hitl_call_runs_and_receives_the_decision() -> None:
    executor = _Executor()
    decision = DecisionBody(tool_call_id="c1", tool_name="hitl_tool", decision="approve")

    await tool_batch_runner.run_tool_batch(
        _batch_rc(executor),
        [ToolCall(tool_name="hitl_tool", arguments="{}", call_id="c1")],
        "step-1",
        ToolBatch(),
        decision,
    )

    [context] = executor.contexts
    assert context.user_decision == decision


# ---------------------------------------------------------------------------
# 占位：投影为非终态 pending
# ---------------------------------------------------------------------------


def test_awaiting_placeholder_projects_pending_and_suspends() -> None:
    observation = tool_awaiting_decision("hitl_tool", "c1")
    event = terminal_event(observation, task_id=1, run_id=2, step_id="step-1", tool_call_id="c1")

    assert observation.suspend is True
    assert "awaiting the user's decision" in observation.content
    assert event.status == "pending"


def test_running_part_can_move_to_pending_then_to_completed() -> None:
    state = {
        "messages": [
            {
                "id": "m1",
                "role": "assistant",
                "runId": 2,
                "outcome": None,
                "parts": [
                    {
                        "type": "tool-call",
                        "toolCallId": "c1",
                        "toolName": "hitl_tool",
                        "status": "running",
                        "args": {},
                        "isError": False,
                    }
                ],
            }
        ],
        "activity": None,
    }
    base = ("messages", 0, "parts", 0)
    for status in ("pending", "completed"):
        mutations = plan_tool_call_status_changed(
            ToolCallStatusChangedEvent(task_id=1, run_id=2, tool_call_id="c1", status=status),
            state,
        )
        assert {m.path: m.value for m in mutations}[(*base, "status")] == status
        state["messages"][0]["parts"][0]["status"] = status


# ---------------------------------------------------------------------------
# prepare_input：决定优先、每轮一条、先确认再执行
# ---------------------------------------------------------------------------


def _prepare_rc(acked: dict[int, TaskMessage], decision_mode: str | None = "execute"):
    tool = _definition("hitl_tool", hitl=True)
    if decision_mode != "execute":
        tool = dataclasses.replace(tool, user_decision=decision_mode)
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
async def test_approval_is_taken_first_and_routes_to_tools_without_the_model() -> None:
    decision = _decision("approve")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(_notice("earlier notice"))
    rc.mailbox.post(decision)
    context = _Context()

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.TOOLS
    assert result["batch"].calls == [_CALL]
    assert DecisionBody.model_validate(result["batch"].decision) == decision.body
    # 先确认并写下决定，其余消息留到工具执行完再写入。
    [written] = context.added
    assert "<user_decision" in written["message"]["content"]
    assert rc.mailbox.pop().body == _notice("earlier notice").body


@pytest.mark.usefixtures("isolated_storage")
async def test_accepting_a_decision_claims_the_placeholder_in_the_same_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接收决定即把等待占位推到 running（与确认同一事务）：执行期间迟到的重复决定不再被认作在等待。"""
    emitted: list[Any] = []
    monkeypatch.setattr(prepare_input_node, "emit", emitted.append)
    decision = _decision("approve")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(decision)
    context = _Context(placeholder=ToolMessageMetadata(status="pending"))

    await _prepare(rc, context)

    [(call_id, metadata)] = context.updated
    assert (call_id, metadata.status) == ("call-hitl", "running")
    [session] = context.update_sessions
    assert session is not None  # 加入了确认的事务，而不是提交后再单独写
    [event] = emitted
    assert (event.tool_call_id, event.status) == ("call-hitl", "running")


@pytest.mark.usefixtures("isolated_storage")
async def test_rejection_of_an_execute_tool_also_routes_to_the_tool() -> None:
    """``execute`` 类工具的驳回同样回到工具：它得落定自己的事实，占位也只有执行结果能收尾。"""
    decision = _decision("reject")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(_notice("note"))
    rc.mailbox.post(decision)
    context = _Context()

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.TOOLS
    assert DecisionBody.model_validate(result["batch"].decision).decision == "reject"
    [written] = context.added
    assert "<user_decision" in written["message"]["content"]
    assert rc.mailbox.pop().body == _notice("note").body


@pytest.mark.usefixtures("isolated_storage")
async def test_reply_tool_approval_goes_to_the_model_not_the_tool() -> None:
    """``reply`` 类工具（如意图澄清）的「同意」是给模型的回答，不执行原调用。"""
    decision = _decision("approve")
    rc = _prepare_rc({5: decision}, decision_mode="reply")
    rc.mailbox.post(decision)
    context = _Context()

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.MODEL
    [written] = context.added
    assert "<user_decision" in written["message"]["content"]


@pytest.mark.usefixtures("isolated_storage")
async def test_only_one_executable_decision_is_taken_per_round() -> None:
    first, second = _decision("approve", message_id=5), _decision("approve", message_id=6)
    rc = _prepare_rc({5: first, 6: second})
    rc.mailbox.post(first)
    rc.mailbox.post(second)
    context = _Context()

    result = await _prepare(rc, context)

    # 本轮只执行第一条；第二条留在 mailbox，下一轮再取。
    assert result["next_node"] == ReactRoute.TOOLS
    assert len(context.added) == 1
    assert rc.mailbox.pop() is second


@pytest.mark.usefixtures("isolated_storage")
async def test_decision_already_acked_elsewhere_does_not_execute() -> None:
    rc = _prepare_rc({})
    rc.mailbox.post(_decision("approve"))
    context = _Context()

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.MODEL
    assert context.added == []


@pytest.mark.usefixtures("isolated_storage")
async def test_approval_of_a_call_missing_from_context_goes_to_the_model() -> None:
    decision = _decision("approve")
    rc = _prepare_rc({5: decision})
    rc.mailbox.post(decision)
    context = _Context(call=None)

    result = await _prepare(rc, context)

    assert result["next_node"] == ReactRoute.MODEL
    assert len(context.added) == 1


# ---------------------------------------------------------------------------
# observe：结果追加为消息，占位只更新展示
# ---------------------------------------------------------------------------


def test_decision_result_is_appended_and_placeholder_only_gets_display_update() -> None:
    events: list[Any] = []
    rc = runtime_stub(event_sink=events.append)
    context = _Context()
    observation = tool_success(
        "hitl_tool", "team started", tool_call_id="call-hitl", display_data={"kind": "probe"}
    )
    batch = ToolBatch(
        calls=[_CALL],
        observations=[dataclasses.asdict(observation)],
        decision={"tool_call_id": "call-hitl", "tool_name": "hitl_tool", "decision": "approve"},
    )

    with execution_scope(rc, context):
        settle_decision_observation(batch, step_id="step-1")

    [appended] = context.added
    assert appended["message"]["role"] == "user"
    assert "<tool_result" in appended["message"]["content"]
    assert "team started" in appended["message"]["content"]
    assert appended["project_to_transport"] is False
    [(call_id, metadata)] = context.updated
    assert (call_id, metadata.status) == ("call-hitl", "completed")
    assert [event.status for event in events] == ["completed"]



# ---------------------------------------------------------------------------
# context：跨 Run 找回原调用；只替换展示 metadata
# ---------------------------------------------------------------------------


def test_context_finds_call_and_updates_only_tool_result_metadata() -> None:
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    replaced: list[Any] = []
    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
    manager.current_task_id = 1
    manager.context_store = SimpleNamespace(
        replace_message=lambda record, **_kw: replaced.append(record)
    )
    placeholder = {"role": "tool", "tool_call_id": "call-hitl", "content": "awaiting"}
    manager._entries = [
        ContextEntry({"role": "assistant", "content": None, "tool_calls": [_CALL]}, 1, 3),
        ContextEntry(
            placeholder, 1, 4, metadata=ToolMessageMetadata(status="pending", hidden=True)
        ),
        ContextEntry({"role": "user", "content": "later"}, 2, 5),
    ]

    assert manager.find_tool_call("call-hitl") == _CALL
    assert manager.find_tool_call("missing") is None

    written = manager.update_tool_result_metadata(
        "call-hitl", ToolMessageMetadata(status="completed")
    )
    [record] = replaced
    assert (record.sequence, record.run_id, record.message) == (4, 1, placeholder)
    assert (written.status, written.hidden) == ("completed", True)
    with pytest.raises(KeyError):
        manager.update_tool_result_metadata("missing", ToolMessageMetadata(status="completed"))


def test_decision_result_without_display_keeps_the_placeholder_card() -> None:
    """结果不带展示数据时沿用占位的：HITL 卡片（如 Team 方案预览）由占位决定。"""
    from app.core.context.context_entry import ContextEntry
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager

    manager = RuntimeMessageContextManager.__new__(RuntimeMessageContextManager)
    manager.current_task_id = 1
    manager.context_store = SimpleNamespace(replace_message=lambda _record, **_kw: None)
    manager._entries = [
        ContextEntry(
            {"role": "tool", "tool_call_id": "call-hitl", "content": "awaiting"},
            1,
            4,
            metadata=ToolMessageMetadata(
                status="pending", display_data={"kind": "probe"}, team_run_id=17
            ),
        ),
    ]

    written = manager.update_tool_result_metadata(
        "call-hitl", ToolMessageMetadata(status="completed")
    )

    assert (written.status, written.display_data, written.team_run_id) == (
        "completed",
        {"kind": "probe"},
        17,
    )
