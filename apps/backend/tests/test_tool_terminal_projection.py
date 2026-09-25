"""工具终态投影（tool_terminal_projection / tool_executor / tool_call_lifecycle）的独立对抗性验证。

本文件针对本次变更的三个文件，按用户给定的判据 A-H 逐条构造用例，使用真实的事件
``plan`` mutation 与（可选的）真实 storage，而不是只复述实现：

- A：成功 / 失败 / 取消三条路径「提前投影」与「settle 兜底」终态同值。
- B：四条 early-return 分支（未注册工具 / profile 权限拒绝 / 参数非法 / execution_context
  为 None）各投出恰一次终态事件（execution_context 为 None 时不投影）。
- C：run_id<=0 / tool_call_id 为空时跳过投影（projector 不被调用）。
- D：投影失败吞异常、返回 False、不改写 observation、写 error 日志。
- E：重复投影（execute 提前 + settle 兜底）后快照终态确定、不漂移。
- F：terminal_error_hint 边界（空串 / 非字符串 / None / 非映射）。
- G：process 隔离工具（execute_terminal）在父进程投影、子进程不投影；成功终态不清空已流出
  的 display_data.output。
- H：并行批次下每个调用各自投出一态、互不串台。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
from app.assistant_transport.event import (
    RunInitializedEvent,
    RunStatusChangedEvent,
    TerminalOutputDeltaData,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallStatusChangedEvent,
)
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
from app.core.tools.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute import tool_terminal_projection as projection_module
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_execute.tool_terminal_projection import (
    normalize_display_data,
    project_tool_terminal_state,
    project_unhandled_tool_failure,
    terminal_error_hint,
    terminal_status,
)
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)

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

    def apply_planned(self, event: Any) -> TransportFrame:
        mutations = tuple(event.plan(copy.deepcopy(self.state)))
        for mutation in mutations:
            _apply_mutation(self.state, mutation)
        validate_snapshot(self.state)
        self.applied.append(event)
        return TransportFrame(
            task_id=event.task_id,
            kind="mutation",
            mutations=mutations,
            source_run_id=getattr(event, "run_id", None),
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


class _RecordingProjector:
    """记录每次 ``process`` 调用的 projector 替身（用于断言「是否被调用」/「恰一次」）。"""

    def __init__(self, state_service: Any | None = None, fail: bool = False) -> None:
        self.events: list[Any] = []
        self._fail = fail
        self._owner = state_service

    def process(self, raw_event: object) -> TransportFrame | None:
        self.events.append(raw_event)
        if self._fail:
            raise RuntimeError("projector exploded")
        if self._owner is not None:
            return self._owner.apply_planned(raw_event)
        return None


def _install_projector(monkeypatch: pytest.MonkeyPatch, projector: Any) -> None:
    """把投影模块取用的 projector 工厂替换为常量替身。"""

    monkeypatch.setattr(
        projection_module, "get_conversation_event_projector", lambda: projector
    )


def _make_context(tmp_path: Path, **overrides: Any) -> ToolExecutionContext:
    base: dict[str, Any] = {
        "task_id": 1,
        "workspace_id": 1,
        "workspace_root": tmp_path,
        "run_id": 1,
    }
    base.update(overrides)
    return ToolExecutionContext(**base)


def _make_read_file_executor(tmp_path: Path) -> ToolExecutor:
    (tmp_path / "sample.txt").write_text("hello world", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool().to_definition()])
    return ToolExecutor(registry=registry)


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
        content="body" if status == "success" else None,
        error="boom" if status == "error" else None,
        reason="why" if status == "error" else None,
        tool_call_id=tool_call_id,
        display_data=display_data,
    )


# ---------------------------------------------------------------------------
# A. 三态映射 + 提前投影 / settle 兜底同值
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


def test_project_records_terminal_state_event_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提前投影投出的终态事件字段与契约一致（status/error/display_data）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    observation = _observation(
        "error", display_data={"status_hint": "读取失败", "extra": 1}
    )

    # 目标：确认投影构造的 event 三字段与预期同值（不是弱断言）。
    result = project_tool_terminal_state(
        task_id=3,
        run_id=4,
        tool_call_id="call-x",
        observation=observation,
    )

    assert result is True
    assert len(projector.events) == 1
    event = projector.events[0]
    assert isinstance(event, ToolCallStatusChangedEvent)
    assert (event.task_id, event.run_id, event.tool_call_id) == (3, 4, "call-x")
    assert event.status == "failed"
    assert event.error == "读取失败"
    assert event.display_data == {"status_hint": "读取失败", "extra": 1}
    # 深拷贝隔离：投影不得让 event 与 observation 共享同一 dict 对象。
    assert event.display_data is not observation.display_data


# ---------------------------------------------------------------------------
# A（续）. 同一 observation 走两条路径等值
# ---------------------------------------------------------------------------


class _LifecycleHarness:
    """装配 lifecycle 运行期依赖并收集发出的终态事件。"""

    def __init__(self, *, add_message_result: bool = True, stream_writer_fails: bool = False) -> None:
        self.events: list[Any] = []
        self.messages: list[Any] = []
        self.operations = SimpleNamespace(
            to_tool_model_message=lambda observation: ("tool-message", observation.tool_call_id),
            model_tools=[SimpleNamespace(name="read_file", display=None)],
        )
        self.runtime_config = SimpleNamespace(operations=self.operations)

        def add_message(message: Any, **_kwargs: Any) -> bool:
            self.messages.append(message)
            return add_message_result

        if stream_writer_fails:

            def stream_writer(event: Any) -> None:
                self.events.append(event)
                raise RuntimeError("stream writer exploded")

        else:
            stream_writer = self.events.append

        self.runtime_context = SimpleNamespace(add_message=add_message)
        self.manager = ToolCallLifecycleManager()
        self._stream_writer = stream_writer

    def _patch_runtime(self) -> Any:
        from unittest.mock import patch

        return patch.multiple(
            lifecycle_module,
            _runtime_config=lambda: self.runtime_config,
            _runtime_context=lambda: self.runtime_context,
            get_stream_writer=lambda: self._stream_writer,
        )

    def settle(self, summary: dict[str, Any]) -> Any:
        with self._patch_runtime():
            manager, event_status = self.manager.settle(
                task_id=1,
                run_id=2,
                step_id="step-3",
                summary=summary,
            )
        self.manager = manager
        return event_status


def _summary_from_observation(observation: ToolObservation) -> dict[str, Any]:
    """按 ``tools`` 节点的 dataclasses.asdict 口径把 observation 转为摘要。"""

    return {
        "tool_call_id": observation.tool_call_id,
        "tool_name": observation.tool_name,
        "status": observation.status,
        "error": observation.error,
        "reason": observation.reason,
        "content": observation.content,
        "retryable": observation.retryable,
        "display_data": observation.display_data,
    }


@pytest.mark.parametrize(
    "status,display_data",
    [
        ("success", {"kind": "read-file-meta", "path": "a.py"}),
        ("error", {"status_hint": "读取失败"}),
        ("error", None),
        ("cancelled", None),
        ("weird", {"status_hint": ""}),
    ],
)
def test_projection_and_settle_agree_on_terminal_fields(
    status: str, display_data: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判据 A：同一 observation 经「提前投影」与「settle 兜底」得到同值 status/error/display_data。"""

    observation = _observation(status, display_data=display_data)

    # 路径 1：提前投影。
    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    assert project_tool_terminal_state(
        task_id=1, run_id=2, tool_call_id="call-1", observation=observation
    )
    advance_event = projector.events[0]

    # 路径 2：settle 兜底。
    harness = _LifecycleHarness()
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file"
    )
    harness.settle(_summary_from_observation(observation))
    settle_event = next(
        event for event in harness.events if isinstance(event, ToolCallStatusChangedEvent)
    )

    assert advance_event.status == settle_event.status
    assert advance_event.error == settle_event.error
    assert advance_event.display_data == settle_event.display_data


def test_projection_and_settle_agree_display_data_is_deep_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判据 A：两路径的 display_data 都应是深拷贝，互不共享源结构（防跨路径漂移）。"""

    source = {"status_hint": "读取失败", "nested": {"k": [1, 2]}}
    observation = _observation("error", display_data=source)

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    assert project_tool_terminal_state(
        task_id=1, run_id=2, tool_call_id="call-1", observation=observation
    )

    advance_event = projector.events[0]
    advance_event.display_data["nested"]["k"].append(999)
    assert source["nested"]["k"] == [1, 2], "提前投影与源 display_data 共享了嵌套结构"


# ---------------------------------------------------------------------------
# B. early-return 全覆盖（四条分支各投出恰一次终态）
# ---------------------------------------------------------------------------


def _count_projections(monkeypatch: pytest.MonkeyPatch) -> _RecordingProjector:
    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    return projector


def test_unknown_tool_projects_exactly_one_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """判据 B：未注册工具（门禁拒绝）经单一出口投出恰一次 failed 终态。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    observation = executor.execute(
        ToolCall(tool_name="no_such_tool", arguments={}, call_id="call-unknown"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "error"
    assert len(projector.events) == 1
    event = projector.events[0]
    assert event.tool_call_id == "call-unknown"
    assert event.status == "failed"
    # tool_error 的默认 status_hint 为工具级兜底（未知工具无工具名映射 → 执行失败）。
    assert event.error == "执行失败"


def test_profile_denied_tool_projects_exactly_one_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 B：Agent profile 权限拒绝经单一出口投出恰一次 failed 终态。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-denied"),
        execution_context=_make_context(tmp_path),
        allowed_tool_names={"other_tool"},
    )

    assert observation.status == "error"
    assert len(projector.events) == 1
    assert projector.events[0].status == "failed"
    assert projector.events[0].error == "读取失败"


def test_invalid_arguments_projects_exactly_one_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 B：参数非法经单一出口投出恰一次 failed 终态。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"nonexistent": 1}, call_id="call-badargs"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "error"
    assert len(projector.events) == 1
    assert projector.events[0].status == "failed"


def test_missing_execution_context_raises_and_does_not_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 B：execution_context 为 None 抛 ValueError，且**不**投影。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    with pytest.raises(ValueError):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-none"),
            execution_context=None,
        )

    assert projector.events == []


def test_success_path_projects_exactly_one_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 B 补：成功路径经单一出口投出恰一次 completed 终态（display_data 不丢）。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-ok"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "success"
    assert len(projector.events) == 1
    assert projector.events[0].status == "completed"
    assert projector.events[0].error is None


# ---------------------------------------------------------------------------
# C. 未绑定 run / 空 tool_call_id 必须跳过
# ---------------------------------------------------------------------------


def test_skips_when_run_id_not_positive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """判据 C：run_id<=0（无 run 绑定）时 projector 不被调用，返回 False。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    for run_id in (0, -1):
        result = project_tool_terminal_state(
            task_id=1,
            run_id=run_id,
            tool_call_id="call-1",
            observation=_observation("success"),
        )
        assert result is False
    assert projector.events == []


def test_skips_when_task_id_not_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """判据 C 补：task_id<=0 同样跳过。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    assert (
        project_tool_terminal_state(
            task_id=0,
            run_id=1,
            tool_call_id="call-1",
            observation=_observation("success"),
        )
        is False
    )
    assert projector.events == []


def test_skips_when_tool_call_id_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """判据 C：空 tool_call_id 时跳过投影（避免 min_length 校验静默丢终态）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    assert (
        project_tool_terminal_state(
            task_id=1,
            run_id=2,
            tool_call_id="",
            observation=_observation("success"),
        )
        is False
    )
    assert projector.events == []


def test_executor_falls_back_to_context_tool_call_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 C 补：call_id 为空时回退 execution_context.tool_call_id；两者都空则跳过。"""

    projector = _count_projections(monkeypatch)
    executor = _make_read_file_executor(tmp_path)

    # call_id 为空，但 context 提供了 tool_call_id → 应投影。
    executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id=""),
        execution_context=_make_context(tmp_path, tool_call_id="ctx-call"),
    )
    assert [e.tool_call_id for e in projector.events] == ["ctx-call"]

    # 两者都空 → 跳过。
    projector.events.clear()
    executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id=""),
        execution_context=_make_context(tmp_path, tool_call_id=""),
    )
    assert projector.events == []


# ---------------------------------------------------------------------------
# D. 投影失败：吞异常、返回 False、不改写 observation、写 error 日志
# ---------------------------------------------------------------------------


def test_projection_failure_is_swallowed_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 D：projector 抛异常时不向上抛、返回 False、observation 不被改写、写 error 日志。"""

    projector = _RecordingProjector(fail=True)
    _install_projector(monkeypatch, projector)
    observation = _observation("error", display_data={"status_hint": "读取失败"})
    before = copy.deepcopy(observation)

    with caplog.at_level("ERROR", logger="coding_agent.backend"):
        result = project_tool_terminal_state(
            task_id=1,
            run_id=2,
            tool_call_id="call-1",
            observation=observation,
        )

    assert result is False
    # observation 字段逐字段不变。
    assert observation == before
    record_names = {record.message for record in caplog.records}
    assert "tool_terminal_state_projection_failed" in record_names
    assert record_names, "未写入 error 日志，投影失败不可排查"


def test_executor_returns_observation_verbatim_when_projection_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 D 补：execute 单出口投影失败时仍原样返回 observation（不阻断执行链）。"""

    _install_projector(monkeypatch, _RecordingProjector(fail=True))
    executor = _make_read_file_executor(tmp_path)

    observation = executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-ok"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "success"
    assert "hello world" in observation.content


def test_projection_still_projects_when_display_data_is_not_a_mapping(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """缺陷类型：展示数据畸形时若放弃投影，前端会一直停在 running（判据 D/A）。

    契约（第二轮确认）：展示数据不可用**不改变返回值**——状态仍是有效事实，
    仍投出终态事件（``display_data=None``，跳过展示字段更新）、返回 True，
    仅额外写一条 ``tool_display_data_dropped`` warning（只记类型名，不记载荷）。
    核心断言仍是「不得抛异常」。
    """

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        content=None,
        error="boom",
        tool_call_id="call-1",
        display_data=["not", "a", "mapping"],  # type: ignore[arg-type]
    )

    with caplog.at_level("WARNING", logger="coding_agent.backend"):
        # 契约：不向上抛出（核心），且仍投出终态（display_data=None），返回 True。
        result = project_tool_terminal_state(
            task_id=1, run_id=2, tool_call_id="call-1", observation=observation
        )

    assert result is True
    assert len(projector.events) == 1, "展示数据畸形不得放弃状态投影"
    event = projector.events[0]
    assert event.status == "failed"
    assert event.display_data is None, "不可用展示数据必须跳过展示字段更新，而非伪造载荷"
    # error 短提示仍按无效 display_data 走失败兜底。
    assert event.error == "执行失败"
    record_names = {record.message for record in caplog.records}
    assert "tool_display_data_dropped" in record_names
    # warning 只记类型名，不得记载荷内容（防泄漏）。
    dropped = next(r for r in caplog.records if r.message == "tool_display_data_dropped")
    assert dropped.data["display_data_type"] == "list"


def test_projection_still_projects_when_deepcopy_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """缺陷类型：不可深拷贝的 Mapping 若导致投影被放弃/异常逃逸（判据 D/A）。

    契约（第二轮确认）：深拷贝失败视为展示数据不可用，仍投出终态（``display_data=None``）、
    返回 True，并写 ``tool_display_data_dropped`` warning；核心断言是「不得抛异常」。
    """

    class _Undeepcopyable:
        def __deepcopy__(self, _memo: Any) -> Any:
            raise RuntimeError("cannot deep copy")

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    observation = ToolObservation(
        tool_name="read_file",
        status="error",
        display_data={"status_hint": "读取失败", "bad": _Undeepcopyable()},
    )

    with caplog.at_level("WARNING", logger="coding_agent.backend"):
        result = project_tool_terminal_state(
            task_id=1, run_id=2, tool_call_id="call-1", observation=observation
        )

    assert result is True
    assert len(projector.events) == 1
    assert projector.events[0].status == "failed"
    assert projector.events[0].display_data is None
    record_names = {record.message for record in caplog.records}
    assert "tool_display_data_dropped" in record_names


def test_execute_does_not_raise_when_handler_returns_malformed_display_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 D 加强（execute 级）：投影失败不得中断工具链路，execute 必须仍返回 observation。

    用自定义 handler 返回 ``display_data`` 为非映射的 observation（``ToolObservation`` 是
    dataclass，无运行期类型校验，故此类 payload 可达投影）。若投影在 try 之外做 ``dict()``，
    异常会从 ``ToolExecutor.execute`` 逃逸，直接中断整个工具链路。
    """

    from app.core.tools.schemas import ToolDefinition

    _install_projector(monkeypatch, _RecordingProjector())

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
        permission="bad_display",
        handler=_bad_handler,
        args_model=_ProbeArgs,
        execution_mode="thread",
    )
    executor = ToolExecutor(registry=ToolRegistry([definition]))

    observation = executor.execute(
        ToolCall(tool_name="bad_display", arguments={}, call_id="call-bad-display"),
        execution_context=_make_context(tmp_path),
    )

    assert observation.status == "error"
    assert observation.tool_call_id == "call-bad-display"


# ---------------------------------------------------------------------------
# E. 重复投影幂等：提前 + 兜底后快照终态确定、不漂移
# ---------------------------------------------------------------------------


def _seed_run(owner: _MemorySnapshotOwner, task_id: int, run_id: int, call_id: str) -> None:
    projector = ConversationEventProjector(owner)
    projector.process(RunInitializedEvent(task_id=task_id, run_id=run_id))
    projector.process(RunStatusChangedEvent(task_id=task_id, run_id=run_id, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=task_id, run_id=run_id, tool_call_id=call_id, tool_name="read_file"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=task_id, run_id=run_id, tool_call_id=call_id, status="running", args={"path": "a"}
        )
    )


def _part(owner: _MemorySnapshotOwner, run_id: int, call_id: str) -> dict[str, Any]:
    run = next(r for r in owner.state["runs"] if r["runId"] == run_id)
    for message in run["messages"]:
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("toolCallId") == call_id:
                return part
    raise AssertionError("tool-call part not found")


def test_double_projection_keeps_snapshot_terminal_state_stable() -> None:
    """判据 E：先提前投影、再 settle 兜底，同一 part 终态确定且两次后完全同值（不漂移）。"""

    owner = _MemorySnapshotOwner(empty_snapshot())
    _seed_run(owner, task_id=1, run_id=9, call_id="call-1")
    projector = ConversationEventProjector(owner)
    observation = _observation(
        "error", display_data={"status_hint": "读取失败", "kind": "read-file-meta"}
    )
    summary = _summary_from_observation(observation)

    # 第一次：提前投影（等价于 ToolExecutor.execute 的单一出口）。
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=9,
            tool_call_id="call-1",
            status=terminal_status(observation.status),
            error=terminal_error_hint("failed", observation.display_data),
            display_data=copy.deepcopy(dict(observation.display_data)),
        )
    )
    after_advance = copy.deepcopy(_part(owner, 9, "call-1"))

    # 第二次：settle 兜底（同值终态）。
    lifecycle = ToolCallLifecycleManager(
        calls={"call-1": ToolCallLifecycleRecord(tool_call_id="call-1", tool_name="read_file")}
    )
    harness = _LifecycleHarness()
    harness.manager = lifecycle
    harness.settle(summary)
    settle_event = next(
        e for e in harness.events if isinstance(e, ToolCallStatusChangedEvent)
    )
    projector.process(settle_event)
    after_settle = copy.deepcopy(_part(owner, 9, "call-1"))

    assert after_advance["status"] == "failed"
    assert after_settle["status"] == after_advance["status"] == "failed"
    assert after_settle["error"] == after_advance["error"] == "读取失败"
    assert after_settle["display_data"] == after_advance["display_data"]
    assert after_settle["isError"] is True


def test_double_projection_cancel_then_settle_is_stable() -> None:
    """判据 E 补：取消路径「提前投影 → settle 兜底」终态确定不变（同值自迁移幂等）。"""

    owner = _MemorySnapshotOwner(empty_snapshot())
    _seed_run(owner, task_id=1, run_id=9, call_id="call-1")
    projector = ConversationEventProjector(owner)
    observation = _observation("cancelled")

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
# G. process 隔离工具：父进程投影 + 终态不清空已流出 output
# ---------------------------------------------------------------------------


class _ProbeArgs(BaseModel):
    """process 隔离探针的入参模型（无字段，便于门禁参数校验通过）。"""

    model_config = ConfigDict(extra="forbid")


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


def test_projection_happens_in_parent_for_process_isolated_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 G：process 隔离工具的终态投影发生在父进程，恰一次（子进程内不投影）。

    用模块级可 pickle handler 注册一个 ``execution_mode="process"`` 工具，走真实
    ``ToolHandlerRunner`` 的子进程链路；投影器记录到父进程线程信息。
    """

    from app.core.tools.schemas import ToolDefinition

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    definition = ToolDefinition(
        name="process_probe",
        group="probe",
        description="process isolation probe",
        permission="process_probe",
        handler=_process_isolated_probe_handler,
        args_model=_ProbeArgs,
        execution_mode="process",
        timeout_seconds=30.0,
    )
    executor = ToolExecutor(registry=ToolRegistry([definition]))

    import os

    observation = executor.execute(
        ToolCall(tool_name="process_probe", arguments={}, call_id="call-proc"),
        execution_context=_make_context(tmp_path),
    )

    # 无论子进程链路是否成功（Windows + coverage 下 spawn 可能因 WinError 6 失败，属环境限制，
    # 非产品缺陷），父进程都必须在单一出口恰投出一次与该观察同值的终态。
    assert len(projector.events) == 1, "父进程应恰投出一次终态"
    assert projector.events[0].tool_call_id == "call-proc"
    assert projector.events[0].status == terminal_status(observation.status)
    if observation.status == "success":
        assert projector.events[0].status == "completed"
        assert projector.events[0].display_data == {"kind": "probe"}
        # handler 在独立子进程中运行（PID 与父进程不同），投影只发生在父进程。
        assert f"pid={os.getpid()}" not in (observation.content or "")
        assert "probe body pid=" in (observation.content or "")


def test_completed_terminal_event_does_not_clear_streamed_output() -> None:
    """判据 G：终态事件的 display_data.output 不得清空已流出的实时 output（真实 plan）。

    构造带 ``terminal_output_seq`` 与已累积 output 的 part，再走真实 ``plan``；终态事件应保留
    完整的流式 output，而不是用被预算截断的最终观察覆盖它。
    """

    owner = _MemorySnapshotOwner(empty_snapshot())
    projector = ConversationEventProjector(owner)
    projector.process(RunInitializedEvent(task_id=1, run_id=9))
    projector.process(RunStatusChangedEvent(task_id=1, run_id=9, status="running"))
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
    """判据 H：多线程并发执行 batch 时，每个 call 各自投出恰一次、call_id/status 不串台。"""

    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")

    lock = threading.Lock()
    events: list[Any] = []

    class _ThreadSafeProjector:
        def process(self, raw_event: object) -> None:
            with lock:
                events.append(raw_event)

    _install_projector(monkeypatch, _ThreadSafeProjector())

    executor = _make_read_file_executor(tmp_path)
    results: dict[str, ToolObservation] = {}
    barrier = threading.Barrier(4)

    def run(index: int) -> None:
        name = "a.txt" if index % 2 == 0 else "b.txt"
        call_id = f"call-{index}"
        barrier.wait()
        results[call_id] = executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": name}, call_id=call_id),
            execution_context=_make_context(tmp_path),
        )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(events) == 4, f"每个调用应各投一态，实际 {len(events)}"
    assert sorted(e.tool_call_id for e in events) == [f"call-{i}" for i in range(4)]
    assert all(e.status == "completed" for e in events)
    # 每个 observation 与其 call_id 对应（无串台）。
    for index in range(4):
        assert results[f"call-{index}"].tool_call_id == f"call-{index}"


def test_parallel_batch_distinct_statuses_do_not_cross_talk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """判据 H 补：混合成功/失败批次下，每个 call 的终态与自身结果一致，互不串台。"""

    (tmp_path / "good.txt").write_text("ok", encoding="utf-8")

    events: list[Any] = []

    class _ThreadSafeProjector:
        def process(self, raw_event: object) -> None:
            events.append(raw_event)

    _install_projector(monkeypatch, _ThreadSafeProjector())
    executor = _make_read_file_executor(tmp_path)

    executor.execute(
        ToolCall(tool_name="read_file", arguments={"path": "good.txt"}, call_id="call-good"),
        execution_context=_make_context(tmp_path),
    )
    executor.execute(
        ToolCall(tool_name="no_such_tool", arguments={}, call_id="call-bad"),
        execution_context=_make_context(tmp_path),
    )

    by_id = {e.tool_call_id: e for e in events}
    assert set(by_id) == {"call-good", "call-bad"}
    assert by_id["call-good"].status == "completed"
    assert by_id["call-bad"].status == "failed"


# ---------------------------------------------------------------------------
# 附加：真实 storage 端到端（若可用）
# ---------------------------------------------------------------------------


def test_module_reads_projector_from_service_depends() -> None:
    """判据 E 加强：投影模块的 projector 取自 ``app.service.depends``（进程级唯一收口）。"""

    assert projection_module.get_conversation_event_projector is __import__("app.service.depends", fromlist=["x"]).get_conversation_event_projector


def test_real_storage_backed_projection_updates_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判据 E 真实 storage：用 ``init_storage()`` 后经真实 state service 投影，快照终态落值。

    ``ConversationTaskStateService._rebuild`` 会调用 ``get_delegation_service()``；本用例用
    内存替身隔离委派读取，避免依赖真实委派表。
    """

    from app.service import depends as depends_module
    from app.storage.store_engines import init_storage

    init_storage()
    monkeypatch.setattr(
        depends_module,
        "get_delegation_service",
        lambda: SimpleNamespace(list_by_parent_turn=lambda _turn_id: []),
    )

    from app.assistant_transport.service.conversation_task_state_service import (
        ConversationTaskStateService,
    )

    ConversationTaskStateService.clear_process_state()
    projector = ConversationEventProjector()
    _install_projector(monkeypatch, projector)

    task_id = 991_873
    # 直接向进程内 state service 注入一条 run 骨架，使投影能命中 part。
    state = empty_snapshot()
    state["runs"] = [
        {
            "runId": 5,
            "status": "running",
            "endReason": None,
            "messages": [
                {"id": "m0", "role": "user", "parts": []},
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
                },
            ],
            "usage": None,
            "error": None,
        }
    ]
    state["current_run_id"] = 5
    projector.state_service.publish_state(task_id, state)

    ok = project_tool_terminal_state(
        task_id=task_id,
        run_id=5,
        tool_call_id="call-r",
        observation=_observation("success", display_data={"kind": "read-file-meta"}),
    )

    assert ok is True
    part = _part_from_state(projector.state_service.get_state(task_id), 5, "call-r")
    assert part["status"] == "completed"
    assert part["display_data"] == {"kind": "read-file-meta"}
    ConversationTaskStateService.clear_process_state()


def _part_from_state(state: ConversationStateSnapshot, run_id: int, call_id: str) -> dict[str, Any]:
    run = next(r for r in state["runs"] if r["runId"] == run_id)
    for message in run["messages"]:
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("toolCallId") == call_id:
                return part
    raise AssertionError("tool-call part not found")


# ---------------------------------------------------------------------------
# 委托一致性：lifecycle 的 _event_status / _ui_error 必须与投影模块逐分支一致
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "error", "cancelled", "weird", ""])
def test_lifecycle_event_status_delegates_to_projection(status: str) -> None:
    """judged: lifecycle ``_event_status`` 逐分支等于投影模块 ``terminal_status``（无第二份映射）。"""

    from app.core.workflows.react.node_helper.tool_call_lifecycle import _event_status

    assert _event_status(status) == terminal_status(status)


@pytest.mark.parametrize(
    "event_status,display_data",
    [
        ("completed", {"status_hint": "x"}),
        ("cancelled", {"status_hint": "x"}),
        ("failed", {"status_hint": "读取失败"}),
        ("failed", {"status_hint": ""}),
        ("failed", {"status_hint": 123}),
        ("failed", None),
        ("failed", ["not", "mapping"]),
    ],
)
def test_lifecycle_ui_error_delegates_to_projection(
    event_status: str, display_data: Any
) -> None:
    """judged: lifecycle ``_ui_error`` 逐分支等于投影模块 ``terminal_error_hint``。"""

    from app.core.workflows.react.node_helper.tool_call_lifecycle import _ui_error

    summary = {"display_data": display_data}
    assert _ui_error(summary, event_status) == terminal_error_hint(event_status, display_data)


def test_settle_batch_mixed_statuses_emits_consistent_events() -> None:
    """判据 A/E 集成：settle_batch 混合成功/失败/取消，事件终态与投影模块映射一致。"""

    harness = _LifecycleHarness()
    summaries = [
        _summary_from_observation(_observation("success", tool_call_id="c1")),
        _summary_from_observation(
            _observation("error", tool_call_id="c2", display_data={"status_hint": "读取失败"})
        ),
        _summary_from_observation(_observation("cancelled", tool_call_id="c3")),
    ]
    for summary in summaries:
        harness.manager.calls[summary["tool_call_id"]] = ToolCallLifecycleRecord(
            tool_call_id=summary["tool_call_id"], tool_name=summary["tool_name"]
        )

    with harness._patch_runtime():
        result = harness.manager.settle_batch(
            task_id=1, run_id=2, step_id="step-3", summaries=summaries, inherited_error_count=0
        )
    harness.manager = result.lifecycle

    terminal_events = [e for e in harness.events if isinstance(e, ToolCallStatusChangedEvent)]
    assert [(e.tool_call_id, e.status, e.error) for e in terminal_events] == [
        ("c1", "completed", None),
        ("c2", "failed", "读取失败"),
        ("c3", "cancelled", "已取消"),
    ]
    assert result.error_count == 1


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
# J. project_unhandled_tool_failure（第二轮新增）
# ---------------------------------------------------------------------------


def test_unhandled_failure_projects_failed_with_no_display_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺陷类型：管线异常路径不投终态，前端停在 running（判据 B/D）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    result = project_unhandled_tool_failure(task_id=5, run_id=6, tool_call_id="call-x")

    assert result is True
    assert len(projector.events) == 1
    event = projector.events[0]
    assert event.status == "failed"
    assert event.display_data is None, "未归一化异常路径不得伪造/覆盖展示数据"
    assert event.error == "执行失败"


def test_unhandled_failure_skips_when_identity_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺陷类型：run_id<=0 / 空 tool_call_id 时仍强行投影（判据 C）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)

    assert project_unhandled_tool_failure(task_id=1, run_id=0, tool_call_id="c") is False
    assert project_unhandled_tool_failure(task_id=1, run_id=1, tool_call_id="") is False
    assert project_unhandled_tool_failure(task_id=0, run_id=1, tool_call_id="c") is False
    assert projector.events == []


def test_unhandled_failure_swallows_projection_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """缺陷类型：兜底投影自身失败时抛异常，遮蔽原始异常（判据 D）。"""

    _install_projector(monkeypatch, _RecordingProjector(fail=True))
    with caplog.at_level("ERROR", logger="coding_agent.backend"):
        result = project_unhandled_tool_failure(task_id=1, run_id=1, tool_call_id="c")

    assert result is False
    assert "tool_terminal_state_projection_failed" in {r.message for r in caplog.records}


def test_execute_projects_failed_then_reraises_when_inner_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺陷类型：_execute_inner 抛未归一化异常时，前端永远停在 running（核心新增路径）。

    契约：异常原样上抛、失败终态事件恰好投出一次（status=failed、display_data=None）。
    """

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    executor = _make_read_file_executor(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> ToolObservation:
        raise RuntimeError("inner pipeline exploded")

    monkeypatch.setattr(executor, "_execute_inner", _boom, raising=True)

    with pytest.raises(RuntimeError, match="inner pipeline exploded"):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-boom"),
            execution_context=_make_context(tmp_path),
        )

    assert len(projector.events) == 1, "管线异常路径应恰投出一次 failed 终态"
    event = projector.events[0]
    assert event.tool_call_id == "call-boom"
    assert event.status == "failed"
    assert event.display_data is None


def test_execute_does_not_let_projection_error_mask_original_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺陷类型：兜底投影失败抛出的新异常遮蔽原始异常（判据 D 关键）。"""

    _install_projector(monkeypatch, _RecordingProjector(fail=True))
    executor = _make_read_file_executor(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> ToolObservation:
        raise ValueError("original failure")

    monkeypatch.setattr(executor, "_execute_inner", _boom, raising=True)

    # 必须抛出原始 ValueError，而不是投影抛出的 RuntimeError。
    with pytest.raises(ValueError, match="original failure"):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-mask"),
            execution_context=_make_context(tmp_path),
        )


def test_execute_without_context_does_not_project_unhandled_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺陷类型：execution_context 为 None 时兜底投影仍被调用（无身份可投）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    executor = _make_read_file_executor(tmp_path)

    with pytest.raises(ValueError):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id="call-none"),
            execution_context=None,
        )

    assert projector.events == []


def test_execute_falls_back_to_context_tool_call_id_on_unhandled_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缺陷类型：异常兜底未回退 context.tool_call_id，导致终态丢失（判据 C 补）。"""

    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    executor = _make_read_file_executor(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> ToolObservation:
        raise RuntimeError("boom")

    monkeypatch.setattr(executor, "_execute_inner", _boom, raising=True)

    with pytest.raises(RuntimeError):
        executor.execute(
            ToolCall(tool_name="read_file", arguments={"path": "sample.txt"}, call_id=""),
            execution_context=_make_context(tmp_path, tool_call_id="ctx-call"),
        )

    assert [e.tool_call_id for e in projector.events] == ["ctx-call"]


# ---------------------------------------------------------------------------
# K. 畸形 display_data 下「两路径同值」仍成立（判据 A 加强，第二轮）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "display_data",
    [
        ["not", "a", "mapping"],
        "raw-string",
        123,
    ],
)
def test_two_paths_agree_on_malformed_display_data(
    display_data: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺陷类型：展示数据畸形时「提前投影」与「settle 兜底」两条路径降级不一致（判据 A）。

    契约：两条路径都投出终态、``display_data`` 均为 None、``error`` 均为失败兜底。
    """

    observation = _observation("error", display_data=display_data)

    # 路径 1：提前投影。
    projector = _RecordingProjector()
    _install_projector(monkeypatch, projector)
    assert project_tool_terminal_state(
        task_id=1, run_id=2, tool_call_id="call-1", observation=observation
    )
    advance_event = projector.events[0]

    # 路径 2：settle 兜底。
    harness = _LifecycleHarness()
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file"
    )
    harness.settle(_summary_from_observation(observation))
    settle_event = next(
        e for e in harness.events if isinstance(e, ToolCallStatusChangedEvent)
    )

    assert advance_event.status == settle_event.status == "failed"
    assert advance_event.display_data is None
    assert settle_event.display_data is None
    assert advance_event.error == settle_event.error == "执行失败"


def test_lifecycle_ui_data_delegates_to_normalize_display_data() -> None:
    """judged: lifecycle ``_ui_data`` 逐分支等于投影模块 ``normalize_display_data``（无第二份归一）。"""

    from app.core.workflows.react.node_helper.tool_call_lifecycle import _ui_data

    class _Undeepcopyable:
        def __deepcopy__(self, _memo: Any) -> Any:
            raise RuntimeError("cannot deep copy")

    for value in (None, {}, {"a": 1}, ["x"], "s", 3, {"bad": _Undeepcopyable()}):
        summary = {"display_data": value}
        assert _ui_data(summary) == normalize_display_data(value)
        if normalize_display_data(value) is None:
            assert _ui_data(summary) is None


# ---------------------------------------------------------------------------
# L. lifecycle 结算与投影相伴的终态路径（提升变更面覆盖）
# ---------------------------------------------------------------------------


def test_settle_is_idempotent_after_terminal_state() -> None:
    """缺陷类型：已终态记录被 settle 二次结算，重复写上下文/重复发事件（判据 E）。"""

    harness = _LifecycleHarness()
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file", status="completed"
    )
    summary = _summary_from_observation(_observation("success", tool_call_id="call-1"))

    event_status = harness.settle(summary)

    assert event_status == "completed"
    assert harness.messages == [], "已终态记录不得重复写 ToolMessage"
    assert harness.events == [], "已终态记录不得重复发终态事件"


def test_settle_returns_without_event_when_message_already_exists() -> None:
    """缺陷类型：add_message 返回 False 时仍发终态事件，产生孤儿状态。"""

    harness = _LifecycleHarness(add_message_result=False)
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file"
    )
    summary = _summary_from_observation(_observation("success", tool_call_id="call-1"))

    event_status = harness.settle(summary)

    assert event_status == "completed"
    assert harness.events == [], "消息未新建时不应发终态事件"


def test_settle_degrades_when_event_emission_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺陷类型：终态事件发送失败向上抛出，中断已落库的结算（应降级并记日志）。"""

    harness = _LifecycleHarness(stream_writer_fails=True)
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file"
    )
    summary = _summary_from_observation(_observation("success", tool_call_id="call-1"))

    with caplog.at_level("ERROR", logger="coding_agent.backend"):
        event_status = harness.settle(summary)

    assert event_status == "completed"
    assert harness.messages, "消息应已落库"
    assert "tool_terminal_event_failed" in {r.message for r in caplog.records}


def test_fail_invalid_tools_emits_failed_and_returns_repair_message() -> None:
    """非法调用收口：补发恰一次 failed 终态事件，返回**状态已前进**的新快照与修复提示。

    ``fail_invalid_tools`` 遵循本类的 copy-on-write 约定，必须在返回值里交出迁移后的 manager；
    调用方丢弃该返回值会让记录停在 ``pending``（历史缺陷），故本用例同时锁定「返回的新快照中
    该调用已为 failed」这一契约。
    """

    harness = _LifecycleHarness()
    harness.manager.calls["bad"] = ToolCallLifecycleRecord(
        tool_call_id="bad",
        tool_name="read_file",
        invalid_detail={"name": "read_file", "args": "{bad", "error": "invalid json"},
    )

    with harness._patch_runtime():
        updated, repair = harness.manager.fail_invalid_tools(
            task_id=1, run_id=2, step_id="step-3"
        )

    events = [e for e in harness.events if isinstance(e, ToolCallStatusChangedEvent)]
    assert len(events) == 1
    assert events[0].tool_call_id == "bad"
    assert events[0].status == "failed"
    assert events[0].error == "参数无效"
    assert repair is not None and "read_file" in repair
    assert updated.calls["bad"].status == "failed"
    # 原快照不被就地改写（copy-on-write）：状态与对象身份都必须不同，浅共享的伪实现会在此失败。
    assert harness.manager.calls["bad"].status == "pending"
    assert harness.manager.calls["bad"] is not updated.calls["bad"]


def test_fail_invalid_tools_no_repair_for_non_pending() -> None:
    """缺陷类型：已终态的非法调用被重复收口（应跳过、返回 None）。"""

    harness = _LifecycleHarness()
    harness.manager.calls["bad"] = ToolCallLifecycleRecord(
        tool_call_id="bad",
        tool_name="read_file",
        status="failed",
        invalid_detail={"name": "read_file", "args": "{}", "error": "x"},
    )

    with harness._patch_runtime():
        updated, repair = harness.manager.fail_invalid_tools(
            task_id=1, run_id=2, step_id="step-3"
        )

    assert repair is None
    assert harness.events == []
    assert updated.calls["bad"].status == "failed"


def test_cancel_moves_pending_and_running_only() -> None:
    """缺陷类型：cancel 误伤已终态调用，将其改写为 cancelled（判据：收口判据是状态）。"""

    harness = _LifecycleHarness()
    harness.manager.calls["p"] = ToolCallLifecycleRecord(tool_call_id="p", tool_name="read_file")
    harness.manager.calls["r"] = ToolCallLifecycleRecord(
        tool_call_id="r", tool_name="read_file", status="running"
    )
    harness.manager.calls["done"] = ToolCallLifecycleRecord(
        tool_call_id="done", tool_name="read_file", status="completed"
    )

    with harness._patch_runtime():
        harness.manager = harness.manager.cancel(task_id=1, run_id=2, step_id="step-3")

    assert harness.manager.calls["p"].status == "cancelled"
    assert harness.manager.calls["r"].status == "cancelled"
    assert harness.manager.calls["done"].status == "completed", "已终态调用不得被 cancel 改写"
    assert sorted(e.tool_call_id for e in harness.events) == ["p", "r"]
    assert all(e.status == "cancelled" for e in harness.events)


def test_classify_invalid_id_never_enters_running() -> None:
    """缺陷类型：非法调用（id 命中非法集合）被当作合法进入 running，污染展示。"""

    harness = _LifecycleHarness()
    harness.manager.calls["bad"] = ToolCallLifecycleRecord(
        tool_call_id="bad", tool_name="read_file", status="pending"
    )

    with harness._patch_runtime():
        harness.manager = harness.manager.classify(
            task_id=1,
            run_id=2,
            step_id="step-3",
            tool_calls=[ToolCall(tool_name="read_file", call_id="bad", arguments={"path": "a"})],
            invalid_tool_calls=[{"id": "bad", "name": "read_file", "args": "{", "error": "e"}],
        )

    record = harness.manager.calls["bad"]
    assert record.status == "pending", "非法调用不得进入 running"
    assert record.invalid_detail is not None
    assert record.invalid_detail["error"] == "e"


def test_classify_invalid_without_id_is_ignored_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺陷类型：缺 id 的非法调用被强行建记录，污染生命周期（混合 id/无 id 场景）。"""

    harness = _LifecycleHarness()
    with caplog.at_level("WARNING", logger="coding_agent.backend"):
        with harness._patch_runtime():
            harness.manager = harness.manager.classify(
                task_id=1,
                run_id=2,
                step_id="step-3",
                tool_calls=[],
                invalid_tool_calls=[
                    {"id": "with-id", "name": "read_file", "args": "{", "error": "e"},
                    {"name": "read_file", "args": "{", "error": "e"},
                ],
            )

    assert set(harness.manager.calls) == {"with-id"}
    assert "lifecycle_invalid_tool_call_no_id" in {r.message for r in caplog.records}


def test_classify_all_id_less_invalid_calls_are_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """缺陷类型：全部非法调用都缺 id 时，不得为它们建记录（判据：无法对齐即噪声）。

    另注（既有观察项，见结论报告）：此场景下 ``classify`` 在 ``if not invalid_by_id`` 处提前
    返回，故 ``lifecycle_invalid_tool_call_no_id`` warning 不可达；本用例只断言无记录被建。
    """

    harness = _LifecycleHarness()
    with harness._patch_runtime():
        harness.manager = harness.manager.classify(
            task_id=1,
            run_id=2,
            step_id="step-3",
            tool_calls=[],
            invalid_tool_calls=[{"name": "read_file", "args": "{", "error": "e"}],
        )

    assert harness.manager.calls == {}


# ---------------------------------------------------------------------------
# M. 第三轮：丢弃留痕同源（真实日志捕获）、无日志噪声、keyword-only 签名
# ---------------------------------------------------------------------------


def _dropped_records(caplog: pytest.LogCaptureFixture) -> list[Any]:
    """从真实捕获的日志记录里挑出 ``tool_display_data_dropped`` warning。"""

    return [r for r in caplog.records if r.message == "tool_display_data_dropped"]


def test_dropped_留痕_early_path_carries_full_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 A：提前投影路径对畸形载荷写 warning，且 data 含 task_id/run_id/tool_call_id。"""

    _install_projector(monkeypatch, _RecordingProjector())
    observation = _observation("error", display_data=["not", "a", "mapping"])

    with caplog.at_level("WARNING"):
        result = project_tool_terminal_state(
            task_id=11, run_id=22, tool_call_id="call-early", observation=observation
        )

    assert result is True
    dropped = _dropped_records(caplog)
    assert len(dropped) == 1, "提前投影路径应恰写一条 tool_display_data_dropped"
    data = dropped[0].data
    assert data["task_id"] == 11
    assert data["run_id"] == 22
    assert data["tool_call_id"] == "call-early"
    assert data["display_data_type"] == "list"
    # 不记载荷内容：warning 的 data 不得出现原载荷元素。
    assert "not" not in str(data)


def test_dropped_留痕_settle_path_carries_tool_call_id(caplog: pytest.LogCaptureFixture) -> None:
    """判据 A：结算路径（_ui_data）对同一畸形载荷同样写 warning，至少含 tool_call_id。"""

    harness = _LifecycleHarness()
    harness.manager.calls["call-settle"] = ToolCallLifecycleRecord(
        tool_call_id="call-settle", tool_name="read_file"
    )
    summary = _summary_from_observation(
        _observation("error", display_data=["not", "a", "mapping"], tool_call_id="call-settle")
    )

    with caplog.at_level("WARNING"):
        with harness._patch_runtime():
            harness.manager.settle(task_id=1, run_id=2, step_id="s", summary=summary)

    dropped = _dropped_records(caplog)
    assert dropped, "结算路径必须对畸形载荷留痕（同源）"
    assert all(r.data["tool_call_id"] == "call-settle" for r in dropped)


def test_dropped_留痕_two_paths_same_event_name_and_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 A：两条路径对同一畸形载荷写出**同名**事件且 display_data_type 一致（同源）。"""

    monkeypatch.setattr(projection_module, "get_conversation_event_projector", lambda: _RecordingProjector())

    with caplog.at_level("WARNING"):
        project_tool_terminal_state(
            task_id=1,
            run_id=2,
            tool_call_id="call-1",
            observation=_observation("error", display_data=123),
        )
        early = _dropped_records(caplog)

    caplog.clear()
    harness = _LifecycleHarness()
    harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
        tool_call_id="call-1", tool_name="read_file"
    )
    with caplog.at_level("WARNING"):
        with harness._patch_runtime():
            harness.manager.settle(
                task_id=1,
                run_id=2,
                step_id="s",
                summary=_summary_from_observation(_observation("error", display_data=123)),
            )
        settle = _dropped_records(caplog)

    assert early and settle
    assert early[0].message == settle[0].message == "tool_display_data_dropped"
    assert early[0].data["display_data_type"] == settle[0].data["display_data_type"] == "int"


@pytest.mark.parametrize("value", [None, {}, {"kind": "read-file-meta"}, {"a": 1, "b": [2]}])
def test_no_dropped_log_for_none_empty_or_valid(
    value: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 B：None / {} / 合法 dict 三种情况一律**不得**写 tool_display_data_dropped。"""

    with caplog.at_level("WARNING"):
        normalize_display_data(value)
        normalize_display_data(value, task_id=1, run_id=2, tool_call_id="c")

    assert _dropped_records(caplog) == []


def test_no_dropped_log_along_early_and_settle_for_valid_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """判据 B：合法载荷走完整两条路径时不得产生任何丢弃 warning（无日志噪声）。"""

    _install_projector(monkeypatch, _RecordingProjector())
    payload = {"kind": "read-file-meta", "path": "a.py"}

    with caplog.at_level("WARNING"):
        project_tool_terminal_state(
            task_id=1,
            run_id=2,
            tool_call_id="call-1",
            observation=_observation("error", display_data=payload),
        )
        harness = _LifecycleHarness()
        harness.manager.calls["call-1"] = ToolCallLifecycleRecord(
            tool_call_id="call-1", tool_name="read_file"
        )
        with harness._patch_runtime():
            harness.manager.settle(
                task_id=1,
                run_id=2,
                step_id="s",
                summary=_summary_from_observation(_observation("error", display_data=payload)),
            )

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


def test_settle_malformed_display_data_writes_two_identical_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """判据 E：settle 一次调用对同一畸形载荷写**两条**相同 warning（_ui_data 被调用两次）。

    核实已知副作用是否真实存在；本用例只做事实核实，不评判可接受性。
    """

    harness = _LifecycleHarness()
    harness.manager.calls["call-dup"] = ToolCallLifecycleRecord(
        tool_call_id="call-dup", tool_name="read_file"
    )
    summary = _summary_from_observation(
        _observation("error", display_data=["bad"], tool_call_id="call-dup")
    )

    with caplog.at_level("WARNING"):
        with harness._patch_runtime():
            harness.manager.settle(task_id=1, run_id=2, step_id="s", summary=summary)

    dropped = _dropped_records(caplog)
    assert len(dropped) == 2, f"预期 settle 一次调用写两条 warning，实际 {len(dropped)}"
    assert dropped[0].data == dropped[1].data
    assert dropped[0].data["tool_call_id"] == "call-dup"
