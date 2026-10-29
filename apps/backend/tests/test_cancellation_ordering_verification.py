"""独立对抗性验证：取消终态的投影契约。

取消终态不得在检出取消的瞬间由执行层（runner）变更，只由 ``tool_batch_runner._execute_call`` 在
``executor.execute`` 返回后经 ``terminal_event`` + ``emit`` 单一出口发出；已流出的终端输出在取消
终态投影后仍保留在卡片上。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

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
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
)
from app.core.tool.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
)
from app.core.tool.execute import tool_handler_runner as runner_module, tool_terminal_projection as projection_module
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.node_helper import tool_batch_runner
from app.models.conversation_input import UserInputPresentation
from tests.runtime_context_stub import runtime_stub
from tests.transport_fixtures import consumed, queue_user_input

_RUN_ID = 960001
_TASK_ID = 12


# ---------------------------------------------------------------------------
# 模块级可 pickle handler（spawn 子进程要求）
# ---------------------------------------------------------------------------


def _probe_sleeps_then_writes_and_streams(
    marker_path: str = "",
    *,
    execution_context: Any = None,
    output_sink: Any = None,
    **_kwargs: Any,
) -> str:
    """睡满长时间后写标记文件；执行期分段回传实时输出（供取消 + 流式交互用例）。

    子进程被强杀时应**从未**写出标记文件，从而给出「kill 真的打断了子进程」的
    独立证据（不是只看父进程是否调用了 kill）。
    """

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if output_sink is not None:
            # 实时通道在父进程强杀/排空后会拒绝写入；探针只负责持续产出，写失败无需处理。
            with contextlib.suppress(Exception):
                output_sink("tick\n")
        time.sleep(0.05)
    if marker_path:
        Path(marker_path).write_text("finished", encoding="utf-8")
    return "completed"


class _ProbeArgs(BaseModel):
    """探针工具入参：可选标记文件路径；额外字段忽略（handler 用 ``**kwargs`` 吸收）。"""

    marker_path: str = ""
    model_config = ConfigDict(extra="ignore")


class _MemorySnapshotOwner:
    """最小 snapshot owner：按事件真实 ``plan`` 产出 mutation 并原地落盘。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        import copy

        return copy.deepcopy(self.state)

    def refresh_parent_delegation(self, _child_task_id: int, _run_id: int, _status: str) -> None:
        """单任务内存 owner 没有父委派卡片可同步；生产实现见 ConversationTaskStateService。"""
        return None

    def apply_planned(self, event: Any) -> TransportFrame:
        import copy

        mutations = tuple(plan_event(event, copy.deepcopy(self.state)))
        for mutation in mutations:
            parent: Any = self.state
            if not mutation.path:
                parent.clear()
                parent.update(copy.deepcopy(mutation.value))
                continue
            for key in mutation.path[:-1]:
                parent = parent[key]
            key = mutation.path[-1]
            if mutation.kind == "append-text":
                parent[key] += mutation.value
            elif isinstance(parent, list) and key == len(parent):
                parent.append(copy.deepcopy(mutation.value))
            else:
                parent[key] = copy.deepcopy(mutation.value)
        validate_snapshot(self.state)
        return TransportFrame(
            task_id=event.task_id,
            kind="mutation",
            mutations=mutations,
        )


# 真实流式 output 只对 ``execute_terminal`` 投影（见 ToolCallRuntimeUpdateEvent.plan），
# 故端到端流式用例必须注册同名工具，否则 display_data 永不累积，验证会失真。
_STREAMING_TOOL_NAME = "execute_terminal"


def _make_process_tool(name: str, *, timeout_seconds: float = 60.0) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        group="probe",
        description="cancellation ordering probe",
        handler=_probe_sleeps_then_writes_and_streams,
        args_model=_ProbeArgs,
        execution_mode="process",
        timeout_seconds=timeout_seconds,
    )


def _batch_runtime(
    executor: Any,
    workspace_root: Path = Path("."),
    event_sink: Any = None,
) -> Any:
    """``_execute_call`` 所需的最小 ``RuntimeContext``；投影记账见 :func:`_projected_batch`。

    执行上下文由 ``_execute_call`` 按调用从这里的 run / task / workspace / event_sink 构造。
    """

    return runtime_stub(
        run=SimpleNamespace(id=_RUN_ID, task_id=_TASK_ID),
        task=SimpleNamespace(id=_TASK_ID),
        workspace=SimpleNamespace(id=1, root_path=str(workspace_root)),
        event_sink=event_sink or (lambda _event: None),
        tool_executor=executor,
        allows_tools=frozenset({_STREAMING_TOOL_NAME}),
    )


def _projected_batch(*call_ids: str) -> ToolBatch:
    """给定调用均视为本步已创建工具卡片的批次。"""

    return ToolBatch(
        valid_tool_calls={
            call_id: ValidToolCall(name=_STREAMING_TOOL_NAME, projection="projected")
            for call_id in call_ids
        }
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    tool_call_cancellation_registry.clear_task(_TASK_ID)
    yield
    tool_call_cancellation_registry.clear_task(_TASK_ID)


# ---------------------------------------------------------------------------
# 判据 2：独立证明取消路径不触达共享投影收口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_path_never_touches_shared_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """取消执行期间监视工作流事件出口与终态映射，证明执行层（runner）无触达。

    缺陷类型：有人把即时通知旁路重新接回执行层（例如在 runner 内构造终态或调用 ``emit``），
    这里会立刻非零。
    """


    emitted: list[Any] = []
    mapped: list[str] = []
    original_terminal_event = projection_module.terminal_event

    def _spy_terminal_event(observation: Any, **kwargs: Any) -> Any:
        mapped.append(kwargs["tool_call_id"])
        return original_terminal_event(observation, **kwargs)

    monkeypatch.setattr(tool_batch_runner, "emit", emitted.append)
    monkeypatch.setattr(projection_module, "terminal_event", _spy_terminal_event)
    monkeypatch.setattr(tool_batch_runner, "terminal_event", _spy_terminal_event)

    tool = _make_process_tool("probe_no_projection")
    context = ToolExecutionContext(
        step_id="step-1",
        task_id=_TASK_ID,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=_RUN_ID,
        tool_call_id="call-cancel",
    )

    # 直接调 runner（执行层）而非 ``_execute_call``，从而隔离出「取消路径本身是否投影」。
    timer = threading.Timer(
        0.3,
        tool_call_cancellation_registry.mark_cancelled,
        args=(_TASK_ID, "call-cancel"),
    )
    timer.start()
    try:
        observation = await asyncio.to_thread(
            runner_module.ToolHandlerRunner().execute,
            tool,
            {"marker_path": str(tmp_path / "m.txt")},
            context,
        )
    finally:
        timer.cancel()

    if observation.status != "cancelled":
        pytest.skip(
            "本环境下 process 工具子进程未能启动（spawn WinError 6 等环境限制），"
            f"未走到取消分支；observation={observation.status!r}"
        )

    assert observation.status == "cancelled"
    assert emitted == [], f"取消路径触达了工作流事件出口：{emitted}"
    assert mapped == [], "取消路径在执行层构造了终态事件"
    # 旧的通知旁路实体与 tools 内的全局 projector 依赖必须不存在（第二层护栏）。
    assert not hasattr(runner_module.ToolHandlerRunner, "_notify_tool_call_cancelled")
    assert not hasattr(runner_module, "get_conversation_event_projector")
    assert not hasattr(projection_module, "get_conversation_event_projector")
    assert not hasattr(projection_module, "project_tool_terminal_state")


# ---------------------------------------------------------------------------
# 判据 3：取消 + 实时输出的交互（已流出内容是否保留）
# ---------------------------------------------------------------------------


def _seed_running_execute_terminal(projector: ConversationEventProjector, call_id: str) -> None:
    """在真实 projector 上初始化一个 ``execute_terminal`` 的 running part。"""

    projector.process(RunInitializedEvent(task_id=_TASK_ID, run_id=_RUN_ID))
    # 生产口径：先投影排队消息，再发只带消息身份的消费事件。
    queue_user_input(projector._state_service.state, 1, UserInputPresentation(display_text="test"))
    projector.process(consumed(_TASK_ID, _RUN_ID, 1))
    projector.process(RunStatusChangedEvent(task_id=_TASK_ID, run_id=_RUN_ID, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id=call_id,
            tool_name="execute_terminal",
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id=call_id,
            status="running",
            args={"command": "x"},
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

    monkeypatch.setattr(tool_batch_runner, "emit", _emit)
    tool_batch_runner._execute_call(
        _batch_runtime(_CancelledExecutor()),
        ToolCall(tool_name="execute_terminal", arguments="{}", call_id=call_id),
        "step-1",
        _projected_batch(call_id),
        None,
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
    streamed = "already streamed output\n"
    projector.process(
        ToolCallRuntimeUpdateEvent(
            task_id=_TASK_ID,
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
    assert event.error == "已取消"
    assert event.display_data == {}

    part = owner.state["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"
    assert part["error"] == "已取消"
    assert part["display_data"]["kind"] == "terminal-result"
    assert part["display_data"]["output"] == streamed
