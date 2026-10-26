"""工具批次的边界：解析、执行、挂起判定和 canonical 结果投影。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.llm_provider.message_codec import assistant_message
from app.core.tool.schemas import ToolCall, ToolObservation
from app.core.tool.execute import tool_executor as tool_executor_module
from app.models.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.models.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.node_helper import streaming_part_state_machine as streaming
from app.core.workflow.node_helper import tool_batch_runner
from app.core.workflow.node_helper import tool_observation_projection as projection
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.node_helper.common import RunStopped
from app.core.workflow.nodes import observation_node, prepare_input_node, tools_node
from app.models.enums.conversation_run_status import RunTransition
from tests.runtime_context_stub import install_runtime_singletons, runtime_stub
from tests.sdk_fixtures import sdk_completion
from tests.tool_executor_fakes import emit_running


def _raw(call_id: str = "call-1", arguments: str = "{}") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": arguments},
    }


def _state(batch: ToolBatch | None = None) -> ReactExecutionState:
    return ReactExecutionState(
        step_count=1,
        next_node=ReactRoute.TOOLS,
        batch=batch or ToolBatch(),
    )


def test_assistant_message_filters_identity_and_registry_only() -> None:
    calls = [
        _raw("valid"),
        _raw(""),
        {**_raw("ghost"), "function": {"name": "ghost_tool", "arguments": "{}"}},
        _raw("valid", '{"path":'),
    ]
    message = sdk_completion(tool_calls=calls).choices[0].message
    projected = assistant_message(message, {"read_file"})
    assert [call["id"] for call in projected["tool_calls"]] == ["valid", "valid"]
    assert projected["tool_calls"][1]["function"]["arguments"] == '{"path":'


def test_streaming_preview_only_creates_allowed_registered_tool(monkeypatch) -> None:
    events = []
    runtime = SimpleNamespace(
        allows_tools={"read_file"},
        tool_definitions={
            "read_file": SimpleNamespace(name="read_file", display=None),
            "write_file": SimpleNamespace(name="write_file", display=None),
        },
    )
    monkeypatch.setattr(streaming, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(streaming, "emit", events.append)
    parts = streaming.StreamingPartStateMachine(
        task_id=1, run_id=2, step_id="step-1", batch=ToolBatch()
    )

    assert parts.create_tool_call("visible", "read_file") is True
    assert parts.create_tool_call("hidden", "write_file") is False
    assert [event.tool_call_id for event in events] == ["visible"]


def test_tools_node_passes_raw_arguments_and_records_observations(monkeypatch) -> None:
    received = []

    async def run_calls(_rc, calls, _step_id, _batch):
        received.extend(calls)
        return [
            ToolObservation(
                tool_name="read_file", status="success", tool_call_id="call-1", content="ok"
            )
        ]

    runtime = runtime_stub(
        task=SimpleNamespace(id=3),
        run=SimpleNamespace(id=4, task_id=3),
        tool_definitions={"read_file": SimpleNamespace(name="read_file", need_HIL=False)},
    )
    batch = ToolBatch(
        calls=[_raw(arguments='{"path":"a.py"}')],
        valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")},
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(tools_node, "run_tool_batch", run_calls)
    patch = asyncio.run(tools_node._tools_node(_state(batch)))

    assert len(received) == 1
    assert received[0].arguments == '{"path":"a.py"}'
    assert patch["batch"].observations[0]["tool_call_id"] == "call-1"
    assert patch["batch"].suspends() is False
    # 观察只写进返回的批次副本，调用与投影记账原样保留给 observe 结算。
    assert patch["batch"].valid_tool_calls == batch.valid_tool_calls
    assert batch.observations == []
    assert patch["next_node"] is ReactRoute.OBSERVE


def test_tools_node_hands_banned_registered_call_to_batch(monkeypatch) -> None:
    """tools 节点不按投影状态过滤：未建卡片（banned）的已注册调用同样交给执行器，由门禁拒绝。"""
    received = []

    async def run_calls(_rc, calls, _step_id, _batch):
        received.extend(calls)
        return []

    banned = {**_raw("banned"), "function": {"name": "write_file", "arguments": "{}"}}
    runtime = runtime_stub(
        task=SimpleNamespace(id=3),
        run=SimpleNamespace(id=4, task_id=3),
        tool_definitions={
            "read_file": SimpleNamespace(name="read_file", need_HIL=False),
            "write_file": SimpleNamespace(name="write_file", need_HIL=False),
        },
    )
    batch = ToolBatch(
        calls=[_raw("visible"), banned],
        valid_tool_calls={
            "visible": ValidToolCall(name="read_file", projection="projected"),
            "banned": ValidToolCall(name="write_file", projection="banned"),
        },
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(tools_node, "run_tool_batch", run_calls)

    asyncio.run(tools_node._tools_node(_state(batch)))

    assert [call.call_id for call in received] == ["visible", "banned"]


class _StartingExecutor:
    """执行器替身：记录收到的调用，并像真实执行器一样在「handler 运行前」自行发出 running。"""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls: list[ToolCall] = []

    def execute(self, call, execution_context, allowed_tool_names):
        self.calls.append(call)
        emit_running(call, execution_context)
        self.order.append(f"handler:{call.call_id}")
        return ToolObservation(
            tool_name=call.tool_name,
            status="success",
            tool_call_id=execution_context.tool_call_id,
            content="ok",
        )


def _batch_runtime(executor):
    return runtime_stub(
        task=SimpleNamespace(id=3),
        run=SimpleNamespace(id=4, task_id=3),
        tool_definitions={
            "read_file": SimpleNamespace(name="read_file", parallel_mode="serial"),
            "write_file": SimpleNamespace(name="write_file", parallel_mode="serial"),
        },
        tool_executor=executor,
    )


def _batch(calls: list[dict], projections: dict[str, str]) -> ToolBatch:
    return ToolBatch(
        calls=calls,
        valid_tool_calls={
            call_id: ValidToolCall(name="read_file", projection=projection)
            for call_id, projection in projections.items()
        },
    )


def _tool_calls(raw_calls: list[dict]) -> list[ToolCall]:
    return [
        ToolCall(
            tool_name=raw["function"]["name"],
            arguments=raw["function"]["arguments"],
            call_id=raw["id"],
        )
        for raw in raw_calls
    ]


def test_run_tool_batch_orders_executor_running_before_batch_terminal(monkeypatch) -> None:
    """running 由执行器在 handler 前发出，终态由批次入口在执行后发出，二者落在同一时间线；
    终态只投影本步已建卡片的调用。"""
    order: list[str] = []
    events = []

    def record(event) -> None:
        events.append(event)
        order.append(f"{event.status}:{event.tool_call_id}")

    monkeypatch.setattr(tool_batch_runner, "emit", record)
    monkeypatch.setattr(tool_executor_module, "emit", record)
    raw_calls = [
        _raw("visible", '{"path":"a.py"}'),
        {**_raw("banned"), "function": {"name": "write_file", "arguments": "{}"}},
    ]
    executor = _StartingExecutor(order)
    runtime = _batch_runtime(executor)
    batch = _batch(raw_calls, {"visible": "projected", "banned": "banned"})

    observations = asyncio.run(
        tool_batch_runner.run_tool_batch(runtime, _tool_calls(raw_calls), "step-1", batch)
    )

    assert [call.call_id for call in executor.calls] == ["visible", "banned"]
    assert [observation.tool_call_id for observation in observations] == ["visible", "banned"]
    # 替身执行器对两个调用都发 running（真实执行器会在准入门禁拒绝 banned）；批次入口只为
    # 已建卡片的 visible 发终态。
    assert order == [
        "running:visible",
        "handler:visible",
        "completed:visible",
        "running:banned",
        "handler:banned",
    ]
    assert events[0].step_id == "step-1"


def test_run_tool_batch_projects_only_terminal_when_executor_never_starts(monkeypatch) -> None:
    """执行器未发 running（门禁拒绝等）时批次入口只发终态；执行器异常收口为 failed。"""
    events = []
    monkeypatch.setattr(tool_batch_runner, "emit", events.append)

    class _Executor:
        def execute(self, call, execution_context, allowed_tool_names):
            if call.call_id == "denied":
                return ToolObservation(
                    tool_name=call.tool_name,
                    status="error",
                    content="error: Invalid tool arguments",
                    tool_call_id=call.call_id,
                )
            raise RuntimeError("boom")

    raw_calls = [_raw("denied"), _raw("broken")]
    runtime = _batch_runtime(_Executor())
    batch = _batch(raw_calls, {"denied": "projected", "broken": "projected"})

    observations = asyncio.run(
        tool_batch_runner.run_tool_batch(runtime, _tool_calls(raw_calls), "step-1", batch)
    )

    assert [(event.tool_call_id, event.status) for event in events] == [
        ("denied", "failed"),
        ("broken", "failed"),
    ]
    assert observations[1].status == "error"
    assert observations[1].tool_call_id == "broken"


def test_any_suspending_call_marks_the_whole_batch(monkeypatch) -> None:
    """挂起由工具声明：同批其它调用照常执行与闭合，任一观察带 suspend 即整批挂起。"""

    async def run_calls(_rc, _calls, _step_id, _batch):
        return [
            ToolObservation(tool_name="read_file", status="success", tool_call_id="call-1"),
            ToolObservation(
                tool_name="read_file", status="success", tool_call_id="call-2", suspend=True
            ),
        ]

    runtime = runtime_stub(
        task=SimpleNamespace(id=3),
        run=SimpleNamespace(id=4, task_id=3),
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(tools_node, "run_tool_batch", run_calls)

    patch = asyncio.run(
        tools_node._tools_node(_state(ToolBatch(calls=[_raw("call-1"), _raw("call-2")])))
    )

    assert [item["suspend"] for item in patch["batch"].observations] == [False, True]
    assert patch["batch"].suspends() is True


@pytest.mark.parametrize("suspend", [False, True])
def test_observe_node_only_settles_and_keeps_batch(monkeypatch, suspend) -> None:
    """observe 只结算本批观察，不迁移 Run、不改批次；挂起请求留在批次里由 prepare_input 判定。"""
    settled: list[tuple[ToolBatch, str]] = []
    batch = ToolBatch(
        calls=[_raw()],
        observations=[{"tool_call_id": "call-1", "suspend": suspend}],
    )
    monkeypatch.setattr(
        observation_node,
        "settle_observations",
        lambda settled_batch, *, step_id: settled.append((settled_batch, step_id)),
    )

    patch = asyncio.run(observation_node._observe_node(_state(batch)))

    assert settled == [(batch, "step-1")]
    assert batch.observations == [{"tool_call_id": "call-1", "suspend": suspend}]
    assert patch == {"next_node": ReactRoute.PREPARE_INPUT}


def _gate_runtime(monkeypatch, *, cancelled=False, mailbox_ids=()):
    """prepare_input 关口用的最小运行时：记录状态迁移，并入消息不落库。"""
    transitions: list[tuple[int, RunTransition]] = []
    mailbox = RunMailbox()
    for message_id in mailbox_ids:
        mailbox.post(SimpleNamespace(id=message_id))
    runtime = SimpleNamespace(
        run=SimpleNamespace(id=4, task_id=3),
        task=SimpleNamespace(id=3),
        usage_stats=None,
        mailbox=mailbox,
        is_cancelled=lambda: cancelled,
    )
    monkeypatch.setattr(prepare_input_node, "_runtime_context", lambda: runtime)
    monkeypatch.setattr(
        prepare_input_node.service_depends,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(
            transition=lambda run_id, transition, **_k: transitions.append((run_id, transition))
            or SimpleNamespace(id=run_id)
        ),
    )
    drained: list[list] = []
    monkeypatch.setattr(
        prepare_input_node, "_write_messages", lambda messages: drained.append(messages) or []
    )
    return transitions, drained


def _gate_state(*, suspend: bool, step_count: int = 1, step_limit: int = 10) -> ReactExecutionState:
    """上一批观察带 ``suspend`` 即为挂起请求（见 ``ToolBatch.suspends``）。"""
    return ReactExecutionState(
        step_count=step_count,
        step_limit=step_limit,
        next_node=ReactRoute.PREPARE_INPUT,
        batch=ToolBatch(observations=[{"tool_call_id": "call-1", "suspend": suspend}]),
    )


@pytest.mark.parametrize("mailbox_ids", [(), (None,)])
def test_prepare_input_suspends_without_wakeable_messages(monkeypatch, mailbox_ids) -> None:
    """没有外部消息（或只有 Run 内通知）时落定 suspended、结束驱动、不取 mailbox。"""
    transitions, drained = _gate_runtime(monkeypatch, mailbox_ids=mailbox_ids)

    patch = asyncio.run(prepare_input_node._prepare_input_node(_gate_state(suspend=True)))

    assert transitions == [(4, RunTransition.SUSPEND)]
    assert patch == {"batch": ToolBatch(), "next_node": ReactRoute.END}
    assert drained == []


def test_prepare_input_skips_suspend_when_external_message_waiting(monkeypatch) -> None:
    """挂起前已有外部可唤醒消息：不挂起，直接并入并进入模型。"""
    transitions, drained = _gate_runtime(monkeypatch, mailbox_ids=(None, 7))

    patch = asyncio.run(prepare_input_node._prepare_input_node(_gate_state(suspend=True)))

    assert transitions == []
    assert [[m.id for m in batch] for batch in drained] == [[None, 7]]
    assert patch["next_node"] is ReactRoute.MODEL
    assert patch["batch"] == ToolBatch()


def test_prepare_input_cancel_wins_over_suspend(monkeypatch) -> None:
    """工具要求等待时 Run 恰好被停：按取消落定，不挂起。"""
    transitions, drained = _gate_runtime(monkeypatch, cancelled=True)

    with pytest.raises(RunStopped):
        asyncio.run(prepare_input_node._prepare_input_node(_gate_state(suspend=True)))

    assert transitions == [(4, RunTransition.CANCEL)]
    assert drained == []


def test_prepare_input_suspend_wins_over_step_limit(monkeypatch) -> None:
    """Agent 在等消息时不按步数用完失败；唤醒后回到关口再检查上限。"""
    transitions, _ = _gate_runtime(monkeypatch)

    patch = asyncio.run(
        prepare_input_node._prepare_input_node(
            _gate_state(suspend=True, step_count=1, step_limit=1)
        )
    )

    assert transitions == [(4, RunTransition.SUSPEND)]
    assert patch == {"batch": ToolBatch(), "next_node": ReactRoute.END}


def test_malformed_json_reaches_executor_unparsed(monkeypatch) -> None:
    """tools 节点不解析参数：残缺 JSON 原样交给执行器，由准入门禁形成错误观察。"""
    called = []

    async def run_calls(_rc, calls, _step_id, _batch):
        called.extend(calls)
        return []

    runtime = runtime_stub(
        task=SimpleNamespace(id=3),
        run=SimpleNamespace(id=4, task_id=3),
        tool_definitions={"read_file": SimpleNamespace(name="read_file", need_HIL=False)},
    )
    batch = ToolBatch(
        calls=[_raw(arguments='{"path":')],
        valid_tool_calls={"call-1": ValidToolCall(name="read_file", projection="projected")},
    )
    monkeypatch.setattr(tools_node, "_runtime_context", lambda: runtime)
    install_runtime_singletons(runtime)
    monkeypatch.setattr(tools_node, "run_tool_batch", run_calls)

    asyncio.run(tools_node._tools_node(_state(batch)))

    assert [call.arguments for call in called] == ['{"path":']


def test_internal_artifact_does_not_enter_tool_batch_snapshot() -> None:
    observation = ToolObservation(
        tool_name="read_file",
        status="success",
        tool_call_id="call-1",
        artifact_data={"secret": "internal"},
    )
    assert "artifact_data" not in tools_node._summary(observation)


def test_observation_persists_before_event_and_hides_unprojected_calls(monkeypatch) -> None:
    order = []
    runtime = SimpleNamespace(run=SimpleNamespace(id=4, task_id=3))
    written_metadata = []

    def add_message(_message, *, metadata: object):
        order.append("persist")
        written_metadata.append(metadata)
        return "appended"

    monkeypatch.setattr(projection, "_runtime_context", lambda: runtime)

    install_runtime_singletons(runtime)
    monkeypatch.setattr(
        projection, "_message_context", lambda: SimpleNamespace(add_message=add_message)
    )
    monkeypatch.setattr(projection, "emit", lambda event: order.append(event.tool_call_id))

    def summary(call_id: str) -> dict:
        return {
            "tool_call_id": call_id,
            "tool_name": "read_file",
            "status": "success",
            "content": "ok",
            "display_data": {},
        }

    batch = ToolBatch(
        observations=[summary("visible"), summary("hidden")],
        valid_tool_calls={
            "visible": ValidToolCall(name="read_file", projection="projected"),
            "hidden": ValidToolCall(name="read_file", projection="banned"),
        },
    )
    projection.settle_observations(batch, step_id="step-1")

    assert order == ["persist", "visible", "persist"]
    assert [item.hidden for item in written_metadata] == [False, True]
