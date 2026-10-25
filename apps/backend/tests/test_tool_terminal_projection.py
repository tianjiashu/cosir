"""工具终态映射与单次调用生命周期投影的边界测试。

终态映射（``tool_terminal_projection``）是纯函数；单次调用的 ``running`` / 终态事件由
``tool_batch_runner._execute_call`` 发出（执行器本身不再投影任何 Transport 状态），这里用
``runtime_stub`` 搭最小 Run 作用域，并替换 ``tool_batch_runner.emit`` 捕获事件。
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
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
    ToolObservation,
)
from app.core.tool.execute import tool_terminal_projection as projection_module, tool_executor as tool_executor_module
from app.core.tool.execute.tool_executor import ToolExecutor
from app.core.tool.execute.tool_terminal_projection import (
    normalize_display_data,
    terminal_error_hint,
    terminal_event,
    terminal_status,
)
from app.tools.tool_handler.read_file import ReadFileTool
from app.core.tool.tool_registry import ToolRegistry
from app.models.tool_batch import ToolBatch, ValidToolCall
from app.core.workflow.node_helper import tool_batch_runner
from app.core.workflow.node_helper.common import execution_scope
from tests.runtime_context_stub import runtime_stub
from tests.tool_executor_fakes import emit_running

# 同步用例没有运行中的事件循环；只有进程工具的实时输出与子 Run 调度会用到它。
_NO_LOOP: Any = None

# ---------------------------------------------------------------------------
# 共享测试替身
# ---------------------------------------------------------------------------


class _MemorySnapshotOwner:
    """最小 snapshot owner：按事件真实 ``plan`` 产出 mutation 并原地落盘。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state
        self.applied: list[Any] = []

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        return copy.deepcopy(self.state)

    def refresh_parent_delegation(self, _child_task_id: int, _run_id: int, _status: str) -> None:
        """单任务内存 owner 没有父委派卡片可同步；生产实现见 ConversationTaskStateService。"""
        return None

    def apply_planned(self, event: Any) -> TransportFrame:
        mutations = tuple(plan_event(event, copy.deepcopy(self.state)))
        for mutation in mutations:
            _apply_mutation(self.state, mutation)
        validate_snapshot(self.state)
        self.applied.append(event)
        return TransportFrame(
            task_id=event.task_id,
            kind="mutation",
            mutations=mutations,
        )


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    parent: Any = state
    if not mutation.path:
        parent.clear()
        parent.update(copy.deepcopy(mutation.value))
        return
    for key in mutation.path[:-1]:
        parent = parent[key]
    key = mutation.path[-1]
    if mutation.kind == "append-text":
        parent[key] += mutation.value
    elif isinstance(parent, list) and key == len(parent):
        parent.append(copy.deepcopy(mutation.value))
    else:
        parent[key] = copy.deepcopy(mutation.value)


class _EventRecorder:
    """线程安全的事件记录器；替换 ``tool_batch_runner.emit`` 使用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[ToolCallStatusChangedEvent] = []

    def __call__(self, event: ToolCallStatusChangedEvent) -> None:
        with self._lock:
            self.events.append(event)

    def statuses(self, call_id: str) -> list[str]:
        return [e.status for e in self.events if e.tool_call_id == call_id]


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> _EventRecorder:
    """running 由执行器发出、终态由批次入口发出：两个出口接到同一记录器，按发生顺序断言。"""
    recorder = _EventRecorder()
    monkeypatch.setattr(tool_batch_runner, "emit", recorder)
    monkeypatch.setattr(tool_executor_module, "emit", recorder)
    return recorder


def _make_context(tmp_path: Path, **overrides: Any) -> ToolExecutionContext:
    base: dict[str, Any] = {
        "task_id": 1,
        "workspace_id": 1,
        "workspace_root": tmp_path,
        "run_id": 2,
        "step_id": "step-1",
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _make_read_file_executor(tmp_path: Path) -> ToolExecutor:
    (tmp_path / "sample.txt").write_text("hello world", encoding="utf-8")
    return ToolExecutor(ToolRegistry([ReadFileTool().to_definition()]))


def _runtime(
    tmp_path: Path,
    executor: Any,
    *,
    projected: dict[str, str] | None = None,
    banned: tuple[str, ...] = (),
    allows: frozenset[str] = frozenset({"read_file"}),
    **overrides: Any,
) -> tuple[SimpleNamespace, ToolBatch]:
    """搭一个 Run 作用域替身与本步批次：``projected`` 为本步已建卡片的 call_id → 工具名。"""

    rc = runtime_stub(
        tool_executor=executor,
        workspace=SimpleNamespace(id=1, root_path=str(tmp_path)),
        allows_tools=allows,
        **overrides,
    )
    batch = ToolBatch()
    for call_id, name in (projected or {}).items():
        batch.valid_tool_calls[call_id] = ValidToolCall(name=name, projection="projected")
    for call_id in banned:
        batch.valid_tool_calls[call_id] = ValidToolCall(name="read_file", projection="banned")
    return rc, batch


def _observation(
    status: str,
    *,
    tool_name: str = "read_file",
    display_data: Any = None,
    tool_call_id: str = "call-1",
) -> ToolObservation:
    return ToolObservation(
        tool_name=tool_name,
        status=status,
        content={"success": "body", "error": "error: boom\nreason: why"}.get(status),
        tool_call_id=tool_call_id,
        display_data=display_data,
    )


class _ProbeArgs(BaseModel):
    """探针工具的入参模型（无字段，便于门禁参数校验通过）。"""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# A. 三态映射与终态事件载荷（纯函数）
# ---------------------------------------------------------------------------


def test_terminal_status_maps_three_states() -> None:
    """三态映射：success→completed / cancelled→cancelled / 其余→failed（含未知态）。"""

    assert terminal_status("success") == "completed"
    assert terminal_status("cancelled") == "cancelled"
    assert terminal_status("error") == "failed"
    # 对抗：任意未知字符串也必须落在 failed，而不是抛异常或误判成功。
    assert terminal_status("weird") == "failed"
    assert terminal_status("") == "failed"
    assert terminal_status("SUCCESS") == "failed"


def test_terminal_error_hint_three_states() -> None:
    """error 提示映射：completed→None / cancelled→已取消 / failed→status_hint 或回退。"""

    assert terminal_error_hint("completed", {"status_hint": "x"}) is None
    assert terminal_error_hint("cancelled", {"status_hint": "x"}) == "已取消"
    assert terminal_error_hint("failed", {"status_hint": "命令失败"}) == "命令失败"
    assert terminal_error_hint("failed", None) == "执行失败"


def test_terminal_event_payload_matches_contract() -> None:
    """终态事件字段与契约一致：身份取调用方显式传入值，status/error/display_data 按映射。"""

    observation = _observation(
        "error", display_data={"status_hint": "读取失败", "extra": 1}, tool_call_id="stale-id"
    )

    event = terminal_event(
        observation, task_id=3, run_id=4, step_id="step-7", tool_call_id="call-x"
    )

    assert isinstance(event, ToolCallStatusChangedEvent)
    # 身份不读 observation.tool_call_id（失败工厂未必回填）。
    assert (event.task_id, event.run_id, event.step_id, event.tool_call_id) == (
        3,
        4,
        "step-7",
        "call-x",
    )
    assert event.status == "failed"
    assert event.error == "读取失败"
    assert event.display_data == {"status_hint": "读取失败", "extra": 1}
    # 深拷贝隔离：事件不得与 observation 共享同一 dict 对象。
    assert event.display_data is not observation.display_data


def test_terminal_event_display_data_is_deep_copied() -> None:
    """终态事件的 display_data 是深拷贝，改写事件不得影响源结构（防跨路径漂移）。"""

    source = {"status_hint": "读取失败", "nested": {"k": [1, 2]}}
    event = terminal_event(
        _observation("error", display_data=source),
        task_id=1,
        run_id=2,
        step_id="s",
        tool_call_id="call-1",
    )

    event.display_data["nested"]["k"].append(999)
    assert source["nested"]["k"] == [1, 2], "终态事件与源 display_data 共享了嵌套结构"


@pytest.mark.parametrize(
    ("status", "expected_status", "expected_error"),
    [("success", "completed", None), ("cancelled", "cancelled", "已取消")],
)
def test_terminal_event_success_and_cancelled(
    status: str, expected_status: str, expected_error: str | None
) -> None:
    """成功与取消观察分别映射为 completed（无 error）与 cancelled（已取消）。"""

    event = terminal_event(
        _observation(status, display_data={"kind": "read-file-meta"}),
        task_id=1,
        run_id=2,
        step_id="s",
        tool_call_id="call-1",
    )

    assert event.status == expected_status
    assert event.error == expected_error
    assert event.display_data == {"kind": "read-file-meta"}


def test_terminal_event_keeps_status_when_display_data_is_not_a_mapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺陷类型：展示数据畸形时若放弃终态，前端会一直停在 running。

    契约：状态仍是有效事实，终态照常构造（``display_data=None``，跳过展示字段更新），
    仅额外写一条 ``tool_display_data_dropped`` warning（只记类型名，不记载荷）。
    """

    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        content="error: boom",
        tool_call_id="call-1",
        display_data=["not", "a", "mapping"],  # type: ignore[arg-type]
    )

    with caplog.at_level("WARNING", logger="coding_agent.backend"):
        event = terminal_event(observation, task_id=1, run_id=2, step_id="s", tool_call_id="call-1")

    assert event.status == "failed"
    assert event.display_data is None, "不可用展示数据必须跳过展示字段更新，而非伪造载荷"
    # error 短提示仍按无效 display_data 走失败兜底。
    assert event.error == "执行失败"
    dropped = _dropped_records(caplog)
    assert len(dropped) == 1
    assert dropped[0].data["display_data_type"] == "list"


def test_terminal_event_keeps_status_when_deepcopy_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺陷类型：不可深拷贝的 Mapping 让终态构造异常逃逸。

    契约：深拷贝失败视为展示数据不可用，终态照常构造（``display_data=None``），并写
    ``tool_display_data_dropped`` warning。
    """

    class _Undeepcopyable:
        def __deepcopy__(self, _memo: Any) -> Any:
            raise RuntimeError("cannot deep copy")

    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        display_data={"status_hint": "读取失败", "bad": _Undeepcopyable()},
    )

    with caplog.at_level("WARNING", logger="coding_agent.backend"):
        event = terminal_event(observation, task_id=1, run_id=2, step_id="s", tool_call_id="call-1")

    assert event.status == "failed"
    assert event.display_data is None
    assert _dropped_records(caplog), "深拷贝失败未留痕"


def test_dropped_log_carries_full_identity(caplog: pytest.LogCaptureFixture) -> None:
    """畸形载荷的 warning 含 task_id/run_id/tool_call_id，且不记载荷内容。"""

    observation = _observation("error", display_data=["not", "a", "mapping"])

    with caplog.at_level("WARNING"):
        terminal_event(observation, task_id=11, run_id=22, step_id="s", tool_call_id="call-early")

    dropped = _dropped_records(caplog)
    assert len(dropped) == 1, "应恰写一条 tool_display_data_dropped"
    data = dropped[0].data
    assert data["task_id"] == 11
    assert data["run_id"] == 22
    assert data["tool_call_id"] == "call-early"
    assert data["display_data_type"] == "list"
    # 不记载荷内容：warning 的 data 不得出现原载荷元素。
    assert "not" not in str(data)


def test_projection_module_does_not_reach_global_projector() -> None:
    """边界：tools 的终态映射是纯函数，不得再持有进程级 projector 入口。"""

    assert not hasattr(projection_module, "get_conversation_event_projector")
    assert not hasattr(projection_module, "project_tool_terminal_state")
    assert not hasattr(projection_module, "project_unhandled_tool_failure")


# ---------------------------------------------------------------------------
# E. 重复投影幂等：提前 + 兜底后快照终态确定、不漂移
# ---------------------------------------------------------------------------


def _seed_run(owner: _MemorySnapshotOwner, task_id: int, run_id: int, call_id: str) -> None:
    projector = ConversationEventProjector(owner)
    projector.process(RunInitializedEvent(task_id=task_id, run_id=run_id))
    projector.process(RunStatusChangedEvent(task_id=task_id, run_id=run_id, status="running"))
    owner.state["messages"].append(
        {
            "id": f"assistant-{run_id}",
            "role": "assistant",
            "parts": [],
            "runId": run_id,
            "outcome": None,
        }
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=task_id, run_id=run_id, tool_call_id=call_id, tool_name="read_file"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=task_id,
            run_id=run_id,
            tool_call_id=call_id,
            status="running",
            args={"path": "a"},
        )
    )


def _part(owner: _MemorySnapshotOwner, run_id: int, call_id: str) -> dict[str, Any]:
    for message in owner.state["messages"]:
        if message["runId"] != run_id:
            continue
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("toolCallId") == call_id:
                return part
    raise AssertionError("tool-call part not found")


def test_double_projection_cancel_then_settle_is_stable() -> None:
    """判据 E 补：取消路径「提前投影 → settle 兜底」终态确定不变（同值自迁移幂等）。"""

    owner = _MemorySnapshotOwner(empty_snapshot())
    _seed_run(owner, task_id=1, run_id=9, call_id="call-1")
    projector = ConversationEventProjector(owner)
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=9,
            tool_call_id="call-1",
            status="cancelled",
            error="已取消",
        )
    )
    first = copy.deepcopy(_part(owner, 9, "call-1"))
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=9,
            tool_call_id="call-1",
            status="cancelled",
            error="已取消",
        )
    )
    second = copy.deepcopy(_part(owner, 9, "call-1"))

    assert first["status"] == second["status"] == "cancelled"
    assert first["error"] == second["error"] == "已取消"
    assert first["isError"] == second["isError"] is False


# ---------------------------------------------------------------------------
# F. terminal_error_hint 边界
# ---------------------------------------------------------------------------


def test_terminal_error_hint_empty_string_is_preserved() -> None:
    """判据 F：status_hint 为空串时必须原样返回空串（不得回退为『执行失败』）。"""

    assert terminal_error_hint("failed", {"status_hint": ""}) == ""


def test_terminal_error_hint_non_string_hint_falls_back() -> None:
    """判据 F：status_hint 非字符串（None/int/bytes/对象）时回退『执行失败』。"""

    for hint in (None, 0, 1, b"x", ["a"], object()):
        assert terminal_error_hint("failed", {"status_hint": hint}) == "执行失败"


def test_terminal_error_hint_display_data_none_or_non_mapping() -> None:
    """判据 F：display_data 为 None / 非映射（list/str/自定义对象）时回退『执行失败』。"""

    assert terminal_error_hint("failed", None) == "执行失败"
    for value in (["x"], "str", 42, object()):
        assert terminal_error_hint("failed", value) == "执行失败"


def test_terminal_error_hint_non_failed_status_returns_none() -> None:
    """判据 F 补：非 failed / cancelled 的任意状态（含未知）一律返回 None。"""

    assert terminal_error_hint("completed", {"status_hint": "x"}) is None
    assert terminal_error_hint("running", {"status_hint": "x"}) is None
    assert terminal_error_hint("pending", None) is None


def test_terminal_error_hint_does_not_consume_mapping() -> None:
    """判据 F 补：hint 选择不得消费调用方传入的映射（防副作用）。"""

    class _SpyMapping(dict):
        def __init__(self) -> None:
            super().__init__(status_hint="读取失败")
            self.get_calls = 0

        def get(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            self.get_calls += 1
            return super().get(*args, **kwargs)

    mapping = _SpyMapping()
    assert terminal_error_hint("failed", mapping) == "读取失败"
    assert mapping.get_calls == 1
    assert dict(mapping) == {"status_hint": "读取失败"}, "映射被消费/改写"


def test_terminal_error_hint_mapping_without_hint_key() -> None:
    """判据 F 补：映射存在但无 status_hint 键时回退『执行失败』。"""

    assert terminal_error_hint("failed", {"other": 1}) == "执行失败"


# ---------------------------------------------------------------------------
# B. _execute_call 生命周期：准入后才 running，执行后恰一次终态
# ---------------------------------------------------------------------------


def test_success_emits_running_then_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """成功路径：先 running（携带展示参数），后恰一次 completed（display_data 不丢）。"""

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(tmp_path, _make_read_file_executor(tmp_path), projected={"call-ok": "read_file"})

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-ok"), "step-1", batch, _NO_LOOP
    )

    assert observation.status == "success"
    assert recorder.statuses("call-ok") == ["running", "completed"]
    running, terminal = recorder.events
    # running 展示准入后的最终参数（含 schema 默认值）。
    assert running.args is not None and running.args["path"] == "sample.txt"
    assert (running.task_id, running.run_id, running.step_id) == (1, 2, "step-1")
    assert terminal.error is None
    assert terminal.step_id == "step-1"
    assert isinstance(terminal.display_data, dict) and terminal.display_data, "成功终态丢了展示数据"


def test_running_is_emitted_only_after_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """running 由执行器在交给 handler 前发出：handler 运行时 running 已在、终态尚未发出。"""

    recorder = _install_recorder(monkeypatch)
    seen_during_handler: list[list[str]] = []

    def _handler(*, execution_context: Any, output_sink: Any = None) -> ToolObservation:
        seen_during_handler.append(recorder.statuses("call-probe"))
        return ToolObservation(
            tool_name="probe", status="success", content="ok", display_data={"kind": "probe"}
        )

    definition = ToolDefinition(
        name="probe",
        group="probe",
        description="lifecycle probe",
        handler=_handler,
        args_model=_ProbeArgs,
    )
    rc, batch = _runtime(
        tmp_path,
        ToolExecutor(ToolRegistry([definition])),
        projected={"call-probe": "probe"},
        allows=frozenset({"probe"}),
    )

    tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="probe", arguments="{}", call_id="call-probe"), "s", batch, _NO_LOOP
    )

    assert seen_during_handler == [["running"]]
    assert recorder.statuses("call-probe") == ["running", "completed"]


def test_unknown_tool_emits_failed_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未注册工具（门禁拒绝）：不发 running，恰一次 failed 终态。"""

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path, _make_read_file_executor(tmp_path), projected={"call-unknown": "no_such_tool"}
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="no_such_tool", arguments="{}", call_id="call-unknown"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert recorder.statuses("call-unknown") == ["failed"]
    # 未知工具无工具名映射 → 执行失败。
    assert recorder.events[0].error == "执行失败"


def test_profile_denied_tool_emits_failed_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Agent profile 权限拒绝：不发 running，恰一次 failed 终态（工具级短提示）。"""

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _make_read_file_executor(tmp_path),
        projected={"call-denied": "read_file"},
        allows=frozenset({"other_tool"}),
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-denied"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert recorder.statuses("call-denied") == ["failed"]
    assert recorder.events[0].error == "读取失败"


def test_invalid_arguments_emit_failed_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """参数非法：门禁拒绝，不发 running，恰一次 failed 终态。"""

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(tmp_path, _make_read_file_executor(tmp_path), projected={"call-bad": "read_file"})

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments='{"nonexistent": 1}', call_id="call-bad"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert recorder.statuses("call-bad") == ["failed"]


@pytest.mark.parametrize("arguments", ['{"path": "sample.txt"}', '{"nonexistent": 1}'])
def test_invisible_call_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: str
) -> None:
    """本步未建卡片的调用（未注册工具名，或本轮 banned）：都不发任何生命周期事件。

    未建卡片 ⇔ 未注册或不在本轮准入名单，二者都在准入门禁被拒，执行器因此不会发 running；
    批次入口只为已建卡片的调用发终态。
    """

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _make_read_file_executor(tmp_path),
        banned=("call-banned",),
        allows=frozenset(),
    )

    for tool_name, call_id in (("no_such_tool", "call-unregistered"), ("read_file", "call-banned")):
        tool_batch_runner._execute_call(
            rc, ToolCall(tool_name=tool_name, arguments=arguments, call_id=call_id), "s", batch, _NO_LOOP
        )

    assert recorder.events == []


def test_execute_call_specializes_context_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """执行上下文按调用特化 tool_call_id，不沿用 Run 级共享上下文上的值（防同批串味）。"""

    _install_recorder(monkeypatch)
    received: list[str] = []

    def _handler(*, execution_context: Any, output_sink: Any = None) -> ToolObservation:
        received.append(execution_context.tool_call_id)
        return ToolObservation(tool_name="probe", status="success", content="ok")

    definition = ToolDefinition(
        name="probe",
        group="probe",
        description="context specialization probe",
        handler=_handler,
        args_model=_ProbeArgs,
    )
    rc, batch = _runtime(
        tmp_path,
        ToolExecutor(ToolRegistry([definition])),
        projected={"call-a": "probe"},
        allows=frozenset({"probe"}),
    )

    tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="probe", arguments="{}", call_id="call-a"), "s", batch, _NO_LOOP
    )

    assert received == ["call-a"]


def test_malformed_display_data_still_emits_failed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """handler 返回非映射 display_data：调用照常返回观察，终态仍发出且跳过展示字段。"""

    recorder = _install_recorder(monkeypatch)

    def _bad_handler(**_kwargs: Any) -> ToolObservation:
        return ToolObservation(
            tool_name="bad_display",
            status="error",
            content="boom",
            display_data=["not", "a", "mapping"],  # type: ignore[arg-type]
        )

    definition = ToolDefinition(
        name="bad_display",
        group="probe",
        description="handler returning malformed display_data",
        handler=_bad_handler,
        args_model=_ProbeArgs,
        execution_mode="thread",
    )
    rc, batch = _runtime(
        tmp_path,
        ToolExecutor(ToolRegistry([definition])),
        projected={"call-bad-display": "bad_display"},
        allows=frozenset({"bad_display"}),
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="bad_display", arguments="{}", call_id="call-bad-display"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert recorder.statuses("call-bad-display") == ["running", "failed"]
    assert recorder.events[-1].display_data is None


def test_event_sink_failure_does_not_break_execute_call(tmp_path: Path) -> None:
    """观测是旁路：真实 ``emit`` 下 sink 抛异常，``_execute_call`` 仍原样返回观察。"""

    calls: list[Any] = []

    def _exploding_sink(event: Any) -> None:
        calls.append(event)
        raise RuntimeError("sink exploded")

    rc, batch = _runtime(
        tmp_path,
        _make_read_file_executor(tmp_path),
        projected={"call-ok": "read_file"},
        event_sink=_exploding_sink,
    )

    with execution_scope(rc, SimpleNamespace()):  # type: ignore[arg-type]
        observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments='{"path": "sample.txt"}', call_id="call-ok"), "s", batch, _NO_LOOP
    )

    assert observation.status == "success"
    assert "hello world" in observation.content
    assert [e.status for e in calls] == ["running", "completed"]


# ---------------------------------------------------------------------------
# C. 执行链异常：收口为 error 观察并发出 failed 终态
# ---------------------------------------------------------------------------


class _RaisingExecutor:
    """执行器替身：可选先按真实契约发出 running，再抛出未归一化异常。"""

    def __init__(self, exc: Exception, *, start_first: bool = False) -> None:
        self._exc = exc
        self._start_first = start_first

    def execute(self, call, execution_context, allowed_tool_names):
        if self._start_first:
            emit_running(call, execution_context)
        raise self._exc


def test_executor_exception_becomes_error_observation_and_failed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """缺陷类型：执行链抛未归一化异常时前端永远停在 running。

    契约：异常不向上抛，收口为 error 观察（只带异常类型名），并恰发出一次 failed 终态；
    异常发生在准入前时不发 running。
    """

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _RaisingExecutor(RuntimeError("inner pipeline exploded")),
        projected={"call-boom": "read_file"},
    )

    with caplog.at_level("ERROR"):
        observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments="{}", call_id="call-boom"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert observation.tool_call_id == "call-boom"
    assert observation.content.startswith("error: internal execution error in the tool pipeline: RuntimeError\n")
    assert "inner pipeline exploded" not in observation.content
    assert "hint: do not retry this tool call." in observation.content
    assert recorder.statuses("call-boom") == ["failed"]
    event = recorder.events[0]
    assert event.tool_call_id == "call-boom"
    assert event.error == "读取失败"
    assert "tool_call_internal_error" in {r.message for r in caplog.records}


def test_executor_exception_after_start_closes_running_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已发 running 后执行链抛异常：必须补发 failed 终态封闭卡片。"""

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _RaisingExecutor(ValueError("late failure"), start_first=True),
        projected={"call-late": "read_file"},
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments="{}", call_id="call-late"), "s", batch, _NO_LOOP
    )

    assert observation.content.startswith("error: internal execution error in the tool pipeline: ValueError\n")
    assert recorder.statuses("call-late") == ["running", "failed"]


def test_executor_exception_on_invisible_call_emits_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """未建卡片的调用执行链异常：仍收口为 error 观察，但不发任何事件。

    未建卡片的调用过不了准入门禁，执行器不会为它发 running，替身因此在准入前即抛出。
    """

    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(tmp_path, _RaisingExecutor(RuntimeError("x")))

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments="{}", call_id="call-hidden"), "s", batch, _NO_LOOP
    )

    assert observation.status == "error"
    assert recorder.events == []


# ---------------------------------------------------------------------------
# D. process 隔离工具与待用户决定
# ---------------------------------------------------------------------------


def _process_isolated_probe_handler(
    *, execution_context: Any, output_sink: Any = None
) -> ToolObservation:
    """process 隔离探针 handler：模块级、可 pickle，在子进程内被调用。

    返回体写入执行进程的 ``os.getpid()``，供父进程断言「handler 确在独立子进程内运行」，
    从而把「父进程投影 + 子进程不投影」缩并为可验证的父进程侧证据。
    """

    import os

    return ToolObservation(
        tool_name="process_probe",
        status="success",
        content=f"probe body pid={os.getpid()}",
        tool_call_id="",
        display_data={"kind": "probe"},
    )


def _suspending_probe_handler(
    *, execution_context: Any, output_sink: Any = None
) -> ToolObservation:
    """返回要求挂起的成功观察，验证挂起不推迟 tool part 的终态。"""

    return ToolObservation(
        tool_name="suspend_probe",
        status="success",
        content="preview",
        tool_call_id=execution_context.tool_call_id,
        display_data={"kind": "probe"},
        suspend=True,
    )


def test_projection_happens_in_parent_for_process_isolated_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """process 隔离工具的生命周期事件只在父进程发出：running 与终态各恰一次。

    用模块级可 pickle handler 注册一个 ``execution_mode="process"`` 工具，走真实
    ``ToolHandlerRunner`` 的子进程链路；记录器只存在于父进程。
    """

    import os

    recorder = _install_recorder(monkeypatch)
    definition = ToolDefinition(
        name="process_probe",
        group="probe",
        description="process isolation probe",
        handler=_process_isolated_probe_handler,
        args_model=_ProbeArgs,
        execution_mode="process",
        timeout_seconds=30.0,
    )
    rc, batch = _runtime(
        tmp_path,
        ToolExecutor(ToolRegistry([definition])),
        projected={"call-proc": "process_probe"},
        allows=frozenset({"process_probe"}),
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="process_probe", arguments="{}", call_id="call-proc"), "s", batch, _NO_LOOP
    )

    # 无论子进程链路是否成功（Windows + coverage 下 spawn 可能失败，属环境限制），父进程都必须
    # 恰发出一次 running 与一次与该观察同值的终态。
    assert recorder.statuses("call-proc") == ["running", terminal_status(observation.status)]
    if observation.status == "success":
        assert recorder.events[-1].display_data == {"kind": "probe"}
        # handler 在独立子进程中运行（PID 与父进程不同）。
        assert f"pid={os.getpid()}" not in (observation.content or "")
        assert "probe body pid=" in (observation.content or "")


def test_suspending_tool_still_closes_its_call_in_the_same_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """挂起不改变调用生命周期：调用当步闭合，照常发 running 与终态；是否挂起只由 observe 决定。"""

    recorder = _install_recorder(monkeypatch)
    definition = ToolDefinition(
        name="suspend_probe",
        group="probe",
        description="suspend probe",
        handler=_suspending_probe_handler,
        args_model=_ProbeArgs,
    )
    rc, batch = _runtime(
        tmp_path,
        ToolExecutor(ToolRegistry([definition])),
        projected={"call-suspend": "suspend_probe"},
        allows=frozenset({"suspend_probe"}),
    )

    observation = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="suspend_probe", arguments="{}", call_id="call-suspend"), "s", batch, _NO_LOOP
    )

    assert observation.status == "success"
    assert observation.suspend is True
    assert recorder.statuses("call-suspend") == ["running", "completed"]


def test_completed_terminal_event_does_not_clear_streamed_output() -> None:
    """判据 G：终态事件的 display_data.output 不得清空已流出的实时 output（真实 plan）。

    构造带 ``terminal_output_seq`` 与已累积 output 的 part，再走真实 ``plan``；终态事件应保留
    完整的流式 output，而不是用被预算截断的最终观察覆盖它。
    """

    owner = _MemorySnapshotOwner(empty_snapshot())
    projector = ConversationEventProjector(owner)
    projector.process(RunInitializedEvent(task_id=1, run_id=9))
    projector.process(RunStatusChangedEvent(task_id=1, run_id=9, status="running"))
    owner.state["messages"].append(
        {"id": "assistant-9", "role": "assistant", "parts": [], "runId": 9, "outcome": None}
    )
    projector.process(
        ToolCallCreatedEvent(
            task_id=1, run_id=9, tool_call_id="call-t", tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=1, run_id=9, tool_call_id="call-t", status="running", args={"command": "x"}
        )
    )
    streamed = "full live output\n" * 600  # 多 chunk，确保 seq 递增而非恒为 0
    last_seq = -1
    for seq, start in enumerate(range(0, len(streamed), 4096)):
        last_seq = seq
        projector.process(
            ToolCallRuntimeUpdateEvent(
                task_id=1,
                run_id=9,
                tool_call_id="call-t",
                seq=seq,
                data=TerminalOutputDeltaData(
                    kind="terminal_output_delta", text=streamed[start : start + 4096]
                ),
            )
        )

    streamed_part = _part(owner, 9, "call-t")
    assert streamed_part["display_data"]["output"] == streamed
    assert streamed_part["terminal_output_seq"] == last_seq
    assert last_seq >= 2, "本用例需覆盖多 chunk 场景"

    # 终态事件携带被预算截断的 output，投影必须保留已流出的完整 output。
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=9,
            tool_call_id="call-t",
            status="completed",
            display_data={
                "kind": "terminal-result",
                "output": "bounded final observation",
                "truncated": True,
                "exit_code": 0,
            },
        )
    )

    part = _part(owner, 9, "call-t")
    assert part["status"] == "completed"
    assert part["display_data"]["output"] == streamed, "终态事件清空了已流出的终端输出"
    assert part["terminal_output_seq"] == last_seq, "终态投影不得改写终端输出序号"
    # 终态事件自带的 exit_code 仍应保留（展示事实合并）。
    assert part["display_data"]["exit_code"] == 0


# ---------------------------------------------------------------------------
# H. 并行批次：每个调用各投一态、互不串台
# ---------------------------------------------------------------------------


def test_parallel_batch_projects_each_call_independently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多线程并发执行同批调用时，每个 call 各自 running→completed，call_id 不串台。"""

    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")
    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _make_read_file_executor(tmp_path),
        projected={f"call-{i}": "read_file" for i in range(4)},
    )
    results: dict[str, ToolObservation] = {}
    barrier = threading.Barrier(4)

    def run(index: int) -> None:
        name = "a.txt" if index % 2 == 0 else "b.txt"
        call_id = f"call-{index}"
        barrier.wait()
        results[call_id] = tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments=json.dumps({"path": name}), call_id=call_id), "s", batch, _NO_LOOP
    )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(recorder.events) == 8, f"每个调用应各投两态，实际 {len(recorder.events)}"
    for index in range(4):
        call_id = f"call-{index}"
        assert recorder.statuses(call_id) == ["running", "completed"]
        # 每个 observation 与其 call_id 对应（无串台）。
        assert results[call_id].tool_call_id == call_id


def test_mixed_batch_distinct_statuses_do_not_cross_talk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """混合成功/失败批次下，每个 call 的终态与自身结果一致，互不串台。"""

    (tmp_path / "good.txt").write_text("ok", encoding="utf-8")
    recorder = _install_recorder(monkeypatch)
    rc, batch = _runtime(
        tmp_path,
        _make_read_file_executor(tmp_path),
        projected={"call-good": "read_file", "call-bad": "no_such_tool"},
    )

    tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="read_file", arguments='{"path": "good.txt"}', call_id="call-good"), "s", batch, _NO_LOOP
    )
    tool_batch_runner._execute_call(
        rc, ToolCall(tool_name="no_such_tool", arguments="{}", call_id="call-bad"), "s", batch, _NO_LOOP
    )

    assert recorder.statuses("call-good") == ["running", "completed"]
    assert recorder.statuses("call-bad") == ["failed"]


# ---------------------------------------------------------------------------
# 附加：真实 storage 端到端
# ---------------------------------------------------------------------------


def test_real_storage_backed_terminal_event_updates_snapshot(
    backend_env: None,
    tmp_path: Path,
) -> None:
    """真实 state service：终态事件经真实 projector 投影后，快照终态落值。

    任务运行时空间在构造时会从主库读 task / workspace，因此先经真实服务建出容器，再用它们
    的 id 驱动投影（不硬编码一个不存在的 task_id）。
    """

    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )
    from app.service.depends import get_workspace_service

    ConversationTaskStateService.clear_process_state()
    projector = ConversationEventProjector()

    root = tmp_path / "projection-workspace"
    root.mkdir()
    workspace = get_workspace_service().create_workspace("projection", str(root))
    task = get_workspace_service().create_task(workspace.id, "projection-task")
    task_id = task.id
    # 直接向进程内 state service 注入一条 run 骨架，使投影能命中 part。
    state = empty_snapshot()
    state["messages"] = [
        {"id": "m0", "role": "user", "parts": [], "runId": 5, "outcome": None},
        {
            "id": "m1",
            "role": "assistant",
            "parts": [
                {
                    "type": "tool-call",
                    "toolCallId": "call-r",
                    "toolName": "read_file",
                    "status": "running",
                    "error": None,
                    "isError": False,
                    "presentation": {},
                }
            ],
            "runId": 5,
            "outcome": None,
        },
    ]
    state["activity"] = {
        "runId": 5,
        "status": "running",
        "endReason": None,
        "error": None,
        "usage": None,
        "contextWindowTotal": None,
        "launch": {"modelConfigId": None, "reasoningEffort": None, "banTools": []},
    }
    projector.state_service.publish_state(task_id, state)

    projector.process(
        terminal_event(
            _observation("success", display_data={"kind": "read-file-meta"}),
            task_id=task_id,
            run_id=5,
            step_id="s",
            tool_call_id="call-r",
        )
    )

    part = _part_from_state(projector.state_service.get_state(task_id), 5, "call-r")
    assert part["status"] == "completed"
    assert part["display_data"] == {"kind": "read-file-meta"}
    ConversationTaskStateService.clear_process_state()


def _part_from_state(state: ConversationStateSnapshot, run_id: int, call_id: str) -> dict[str, Any]:
    for message in state["messages"]:
        if message["runId"] != run_id:
            continue
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("toolCallId") == call_id:
                return part
    raise AssertionError("tool-call part not found")


# ---------------------------------------------------------------------------
# I. normalize_display_data 边界（第二轮新增）
# ---------------------------------------------------------------------------


def test_normalize_display_data_none_returns_none() -> None:
    """缺陷类型：None 展示数据被误归一为 {} 而覆盖既有展示字段（判据 I）。"""

    assert normalize_display_data(None) is None


def test_normalize_display_data_empty_dict_returns_independent_dict() -> None:
    """缺陷类型：空 dict 被误当作「不可用」而跳过展示字段（空载荷是合法载荷）。"""

    result = normalize_display_data({})
    assert result == {}
    assert result is not None
    # 深拷贝：返回体不得与输入共享引用（用可变输入验证）。
    source: dict[str, Any] = {}
    normalized = normalize_display_data(source)
    assert normalized is not source


@pytest.mark.parametrize("value", [["a"], "str", 42, b"bytes", 3.14, object()])
def test_normalize_display_data_non_mapping_returns_none(value: Any) -> None:
    """缺陷类型：非映射展示数据未被拦截为 None，导致事件载荷类型违约（判据 I）。"""

    assert normalize_display_data(value) is None


def test_normalize_display_data_mapping_subclass_is_accepted_and_copied() -> None:
    """缺陷类型：Mapping 子类（非 dict）被 isinstance(value, dict) 误拒（判据 I）。"""

    class _MyMapping(dict):
        pass

    source = _MyMapping(status_hint="读取失败")
    result = normalize_display_data(source)
    assert result == {"status_hint": "读取失败"}
    assert isinstance(result, dict)
    # 归一结果必须是普通 dict 的深拷贝，不能仍是子类实例或共享引用。
    assert result is not source


def test_normalize_display_data_deep_copies_nested_structures() -> None:
    """缺陷类型：浅拷贝导致展展示数据与源结构共享嵌套对象（跨路径漂移）。"""

    source = {"nested": {"k": [1, 2]}}
    result = normalize_display_data(source)
    assert result is not None
    result["nested"]["k"].append(999)  # type: ignore[index]
    assert source["nested"]["k"] == [1, 2], "归一结果与源共享了嵌套结构"


def test_normalize_display_data_undeepcopyable_returns_none() -> None:
    """缺陷类型：不可深拷贝的 Mapping 使异常逃逸出归一函数（判据 I/D）。"""

    class _Undeepcopyable:
        def __deepcopy__(self, _memo: Any) -> Any:
            raise RuntimeError("cannot deep copy")

    assert normalize_display_data({"bad": _Undeepcopyable()}) is None


def test_normalize_display_data_is_pure_and_does_not_mutate_input() -> None:
    """缺陷类型：归一时改写调用方入参（投影应是只读旁路）。"""

    source = {"status_hint": "读取失败", "nested": {"k": [1]}}
    before = copy.deepcopy(source)
    normalize_display_data(source)
    assert source == before


# ---------------------------------------------------------------------------
# M. 丢弃留痕：无日志噪声、keyword-only 签名
# ---------------------------------------------------------------------------


def _dropped_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    """从真实捕获的日志记录里挑出 ``tool_display_data_dropped`` warning。"""

    return [r for r in caplog.records if r.message == "tool_display_data_dropped"]


@pytest.mark.parametrize("value", [None, {}, {"kind": "read-file-meta"}, {"a": 1, "b": [2]}])
def test_no_dropped_log_for_none_empty_or_valid(
    value: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 B：None / {} / 合法 dict 三种情况一律**不得**写 tool_display_data_dropped。"""

    with caplog.at_level("WARNING"):
        normalize_display_data(value)
        normalize_display_data(value, task_id=1, run_id=2, tool_call_id="c")

    assert _dropped_records(caplog) == []


def test_normalize_display_data_is_keyword_only() -> None:
    """判据 D：新定位形参为 keyword-only，按位置传入必须 TypeError（防静默错位）。"""

    with pytest.raises(TypeError):
        normalize_display_data({"a": 1}, 1)  # type: ignore[misc]
    with pytest.raises(TypeError):
        normalize_display_data({"a": 1}, 1, 2, "c")  # type: ignore[misc]
    # keyword 传参正常工作且默认值不影响既有单参调用。
    assert normalize_display_data({"a": 1}) == {"a": 1}


def test_normalize_display_data_defaults_are_optional() -> None:
    """判据 D 补：不传定位标识时仍可归一（新形参有默认值，不影响既有调用点）。"""

    assert normalize_display_data({"a": 1}) == {"a": 1}
    assert normalize_display_data({"a": 1}, task_id=1) == {"a": 1}
    assert normalize_display_data({"a": 1}, run_id=2) == {"a": 1}
    assert normalize_display_data({"a": 1}, tool_call_id="c") == {"a": 1}
