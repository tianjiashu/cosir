"""独立对抗性验证：取消终态的投影契约。

取消终态不得在检出取消的瞬间由执行层（runner）变更，只由 ``tools_node._execute_call`` 在
``executor.execute`` 返回后经 ``terminal_event`` + ``emit`` 单一出口发出；已流出的终端输出在取消
终态投影后仍保留在卡片上。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.core.events import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    TerminalCommandStartedData,
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
)
from app.core.file_access.knowledge import FileKnowledge
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.schemas import (
    ToolCall,
)
from app.core.tool.schemas.request_tools import RequestTools
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.nodes import tools_node
from app.models.message_context.message_metadata.tool_message_metadata import ToolMessageMetadata
from app.models.message_context.message_metadata.user_message_metadata import UserInputPresentation
from app.models.session.session_extra import SessionExtra
from tests.runtime_context_stub import admitting, runtime_stub
from tests.transport_fixtures import consumed

# RunInitializedEvent 的 created_at 由领域侧从 Run 记录填入；测试给固定值。
_RUN_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

_RUN_ID = 960001
_SESSION_ID = 12


class _MemorySnapshotOwner:
    """最小 snapshot owner：按事件真实 ``plan`` 产出 mutation 并原地落盘。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _session_id: int) -> ConversationStateSnapshot:
        import copy

        return copy.deepcopy(self.state)

    def apply_planned(self, event: Any) -> TransportFrame:
        import copy

        mutations = tuple(plan_event(event, copy.deepcopy(self.state), {}))
        for mutation in mutations:
            mutation.apply_to(self.state)
        validate_snapshot(self.state)
        return TransportFrame(
            session_id=event.session_id,
            kind="mutation",
            mutations=mutations,
        )


# 真实流式 output 只对 ``execute_terminal`` 投影（见 ToolCallRuntimeUpdateEvent.plan），
# 故端到端流式用例必须注册同名工具，否则 display_data 永不累积，验证会失真。
_STREAMING_TOOL_NAME = "execute_terminal"


def _batch_runtime(
    executor: Any,
    workspace_root: Path = Path("."),
    event_sink: Any = None,
) -> Any:
    """``_execute_call`` 所需的最小 ``RuntimeContext``。

    执行上下文由 ``_execute_call`` 按调用从这里的 run / session / workspace / event_sink 构造。
    """

    return runtime_stub(
        run=SimpleNamespace(
            id=_RUN_ID, session_id=_SESSION_ID, terminal_permission_mode="request_approval"
        ),
        session=SimpleNamespace(id=_SESSION_ID, extra=SessionExtra()),
        workspace=SimpleNamespace(id=1, root_path=str(workspace_root)),
        event_sink=event_sink or (lambda _event: None),
        tool_executor=executor,
        request_tools=admitting(frozenset({_STREAMING_TOOL_NAME})),
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    tool_call_cancellation_registry.clear_session(_SESSION_ID)
    yield
    tool_call_cancellation_registry.clear_session(_SESSION_ID)


# ---------------------------------------------------------------------------
# 判据 3：取消 + 实时输出的交互（已流出内容是否保留）
# ---------------------------------------------------------------------------


def _seed_running_execute_terminal(projector: ConversationEventProjector, call_id: str) -> None:
    """在真实 projector 上初始化一个 ``execute_terminal`` 的 running part。"""

    projector.process(
        RunInitializedEvent(created_at=_RUN_CREATED_AT, session_id=_SESSION_ID, run_id=_RUN_ID)
    )
    # 生产口径：先投影排队消息，再发只带消息身份的消费事件。
    projector.process(consumed(_SESSION_ID, _RUN_ID, 1, UserInputPresentation(text="test")))
    projector.process(
        RunStatusChangedEvent(
            changed_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            status="running",
        )
    )
    projector.process(
        ToolCallCreatedEvent(
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            turn=1,
            tool_call_id=call_id,
            tool_name="execute_terminal",
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            tool_call_id=call_id,
            result=ToolMessageMetadata(status="running"),
        )
    )


def _project_cancelled_via_real_path(
    monkeypatch: pytest.MonkeyPatch,
    projector: ConversationEventProjector,
    call_id: str,
) -> ToolCallStatusChangedEvent:
    """经真实 ``_execute_call`` 终态出口产出取消终态事件并投给 projector。

    这是**取消路径的真实事件来源**：执行器返回 ``ToolObservation.status="cancelled"`` ->
    ``_execute_call`` -> ``terminal_event`` -> ``emit`` -> projector。执行器替换为直接返回取消
    观察的桩（取消观察本身由 ``tool_cancelled`` 产出，与 runner 同源），比手工构造事件更接近
    生产，避免「事件构造与生产不一致」的假阴性。
    """

    observation = tool_cancelled("execute_terminal", tool_call_id=call_id)
    captured: list[ToolCallStatusChangedEvent] = []

    def _emit(event: Any) -> None:
        captured.append(event)
        projector.process(event)

    class _CancelledExecutor:
        def execute(self, _call: ToolCall, **_kwargs: Any) -> Any:
            return observation

    monkeypatch.setattr(tools_node, "emit", _emit)
    tools_node._execute_call(
        _batch_runtime(_CancelledExecutor()),
        ToolCall(tool_name="execute_terminal", arguments="{}", call_id=call_id),
        "step-1",
        None,
        RequestTools(schemas=(), admitted=frozenset({"execute_terminal"})),
        FileKnowledge(),
    )
    assert len(captured) == 1
    return captured[0]


def test_real_cancel_projection_keeps_streamed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消终态经真实路径投影后，已流出的终端输出仍保留在卡片上。

    取消观察的 ``display_data`` 是空 dict（不带成功态展示），终态事件因此携带 ``{}``；
    ``plan_tool_call_status_changed`` 对已流出输出的 terminal-result part 保留实时展示，只叠加
    终态自带字段，不能整体覆盖成空。
    """

    owner = _MemorySnapshotOwner(empty_snapshot())
    projector = ConversationEventProjector(owner)
    _seed_running_execute_terminal(projector, "call-t")
    projector.process(
        ToolCallRuntimeUpdateEvent(
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            tool_call_id="call-t",
            seq=0,
            data=TerminalCommandStartedData(
                kind="terminal_command_started",
                display_data={"kind": "terminal-result", "command": "make", "workdir": "/w"},
            ),
        )
    )
    streamed = "already streamed output\n"
    projector.process(
        ToolCallRuntimeUpdateEvent(
            session_id=_SESSION_ID,
            run_id=_RUN_ID,
            tool_call_id="call-t",
            seq=0,
            data=TerminalOutputDeltaData(kind="terminal_output_delta", text=streamed),
        )
    )
    assert owner.state["messages"][1]["parts"][0]["display_data"]["output"] == streamed

    event = _project_cancelled_via_real_path(monkeypatch, projector, "call-t")

    # 取消事件本身不带成功态展示；保留输出是投影侧的职责。
    assert event.status == "cancelled"
    assert event.result.error == "已取消"
    assert event.result.display_data == {}

    part = owner.state["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"
    assert part["error"] == "已取消"
    assert part["display_data"]["kind"] == "terminal-result"
    assert part["display_data"]["output"] == streamed
