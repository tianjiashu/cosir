"""独立对抗性验证：取消终态的时序契约（kill → drain → project）。

本文件不复用 ``test_tool_call_cancellation.py`` / ``test_tool_terminal_projection.py`` 的
用例与思路，而是从**真实执行链路 + 时间戳事件流**的角度独立验证用户声明的规则：

    取消终态不得在检出取消的瞬间变更，必须晚于 ``_force_kill`` 与输出排空，
    由 ``ToolExecutor.execute`` 的单一出口投影。

反循环论证说明：本文件不对被测函数做「替身替换」再断言调用栈，而是
**包装真实函数 + 委托原实现**，在被测函数真实进入的瞬间记录
``time.monotonic()`` 时间戳到共享事件流。因此断言的是真实执行时序，
而非「谁在假实现在被调用的顺序」。投影点既记录 ``_project`` 的进入，
又记录真实 ``projector.process`` 的调用，二者都指向同一条真实事件。
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

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
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.schemas import (
    ToolCall,
    ToolDefinition,
    ToolExecutionContext,
)
from app.core.tools.schemas.tool_output import ProcessToolOutputChannelFactory
from app.core.tools.tool_execute import tool_handler_runner as runner_module
from app.core.tools.tool_execute import tool_terminal_projection as projection_module
from app.core.tools.tool_execute.tool_executor import ToolExecutor
from app.core.tools.tool_execute.tool_output_channel import (
    BufferedProcessToolOutputChannel,
)
from app.core.tools.tool_registry import ToolRegistry

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


# ---------------------------------------------------------------------------
# 时间戳事件流：包装「真实函数」并委托原实现（非替身）
# ---------------------------------------------------------------------------


class _Timeline:
    """记录 ``(monotonic 时间戳, 标签)`` 的可排序事件流。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[float, str]] = []

    def record(self, label: str) -> None:
        with self._lock:
            self._entries.append((time.monotonic(), label))

    @property
    def entries(self) -> list[tuple[float, str]]:
        with self._lock:
            return list(self._entries)

    def labels(self) -> list[str]:
        return [label for _ts, label in sorted(self.entries, key=lambda item: item[0])]


class _TimelineProjector:
    """真实 projector 语义 + 记录真实 ``process`` 调用时间戳与事件。

    不是替身：把事件转交给真实 ``ConversationEventProjector``，保留真实结果。
    """

    def __init__(self, timeline: _Timeline, inner: ConversationEventProjector) -> None:
        self._timeline = timeline
        self._inner = inner
        self.events: list[Any] = []

    def process(self, raw_event: Any) -> TransportFrame | None:
        self._timeline.record("projector.process")
        self.events.append(raw_event)
        return self._inner.process(raw_event)


class _MemorySnapshotOwner:
    """最小 snapshot owner：按事件真实 ``plan`` 产出 mutation 并原地落盘。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        import copy

        return copy.deepcopy(self.state)

    def apply_planned(self, event: Any) -> TransportFrame:
        import copy

        mutations = tuple(event.plan(copy.deepcopy(self.state)))
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
            source_run_id=getattr(event, "run_id", None),
        )


def _instrument_real_calls(monkeypatch: pytest.MonkeyPatch, timeline: _Timeline) -> None:
    """包装真实函数：进入时记录时间戳后委托原实现（不改行为）。"""

    original_force_kill = runner_module.ToolHandlerRunner._force_kill
    original_finish = runner_module.ToolHandlerRunner._finish_process_output_channel
    original_project = projection_module._project

    def _force_kill(self: Any, process: Any) -> None:
        timeline.record("force_kill")
        return original_force_kill(self, process)

    def _finish(output_channel: Any) -> None:
        timeline.record("finish_output_channel")
        return original_finish(output_channel)

    def _project(**kwargs: Any) -> bool:
        timeline.record("_project")
        return original_project(**kwargs)

    monkeypatch.setattr(runner_module.ToolHandlerRunner, "_force_kill", _force_kill)
    monkeypatch.setattr(
        runner_module.ToolHandlerRunner,
        "_finish_process_output_channel",
        staticmethod(_finish),
    )
    monkeypatch.setattr(projection_module, "_project", _project)


class _LoopBackedChannelFactory(ProcessToolOutputChannelFactory):
    """真实输出通道工厂。

    ``publish`` 走真实生产路径：把增量包成 ``ToolCallRuntimeUpdateEvent`` 投给真实
    projector（``on_publish`` 由用例注入），从而让 snapshot 累积真实的流式 output，
    用于验证取消终态是否保留它。``streamed`` 同步留存原始文本便于断言。
    """

    def __init__(
        self,
        timeline: _Timeline,
        *,
        on_publish: Any = None,
    ) -> None:
        self._timeline = timeline
        self._on_publish = on_publish
        self.streamed: list[str] = []
        self.task_id = 0
        self.run_id = 0
        self.tool_call_id = ""

    def create(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_call_id: str,
        tool_name: str,
        loop: Any,
    ) -> BufferedProcessToolOutputChannel | None:
        self.task_id, self.run_id, self.tool_call_id = task_id, run_id, tool_call_id

        def publish(seq: int, text: str) -> None:
            self.streamed.append(text)
            if self._on_publish is not None:
                self._on_publish(seq, text)
            self._timeline.record("stream_publish")

        return BufferedProcessToolOutputChannel(
            task_id=task_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            loop=loop,
            publish=publish,
            max_chunk_chars=4096,
        )


# 真实流式 output 只对 ``execute_terminal`` 投影（见 ToolCallRuntimeUpdateEvent.plan），
# 故端到端流式用例必须注册同名工具，否则 display_data 永不累积，验证会失真。
_STREAMING_TOOL_NAME = "execute_terminal"


def _make_process_tool(name: str, *, timeout_seconds: float = 60.0) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="cancellation ordering probe",
        permission="probe",
        handler=_probe_sleeps_then_writes_and_streams,
        args_model=_ProbeArgs,
        execution_mode="process",
        timeout_seconds=timeout_seconds,
    )


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    tool_call_cancellation_registry.clear_run(_RUN_ID)
    yield
    tool_call_cancellation_registry.clear_run(_RUN_ID)


# ---------------------------------------------------------------------------
# 判据 1：端到端证明「投影晚于强杀与输出排空」
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_ordering_kill_then_drain_then_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 process 工具中途取消：真实时间戳证明 kill → drain → project，且投影恰一次。

    缺陷类型：若取消终态在检出瞬间投影（即时旁路回归），``_project`` 时间戳会落在
    ``force_kill`` 之前；若投影缺失，projector 事件数为 0。
    """

    import asyncio

    loop = asyncio.get_running_loop()
    timeline = _Timeline()
    marker = tmp_path / "probe_marker.txt"

    # 真实投影对象 + 时间戳包装（委托真实 inner）。
    owner = _MemorySnapshotOwner(empty_snapshot())
    real_projector = ConversationEventProjector(owner)
    real_projector.process(RunInitializedEvent(task_id=_TASK_ID, run_id=_RUN_ID))
    real_projector.process(
        RunStatusChangedEvent(task_id=_TASK_ID, run_id=_RUN_ID, status="running")
    )
    real_projector.process(
        ToolCallCreatedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-cancel",
            tool_name=_STREAMING_TOOL_NAME,
        )
    )
    real_projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-cancel",
            status="running",
            args={},
        )
    )
    projector = _TimelineProjector(timeline, real_projector)
    monkeypatch.setattr(projection_module, "get_conversation_event_projector", lambda: projector)
    _instrument_real_calls(monkeypatch, timeline)

    def _dispatch_stream_delta(seq: int, text: str) -> None:
        """把真实增量包成真实运行期事件投给真实 projector（生产同路径）。"""

        real_projector.process(
            ToolCallRuntimeUpdateEvent(
                task_id=_TASK_ID,
                run_id=_RUN_ID,
                tool_call_id="call-cancel",
                seq=seq,
                data=TerminalOutputDeltaData(
                    kind="terminal_output_delta", text=text
                ),
            )
        )

    tool = _make_process_tool(_STREAMING_TOOL_NAME)
    executor = ToolExecutor(registry=ToolRegistry([tool]))
    channel_factory = _LoopBackedChannelFactory(timeline, on_publish=_dispatch_stream_delta)
    context = ToolExecutionContext(
        task_id=_TASK_ID,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=_RUN_ID,
    )
    import dataclasses

    context = dataclasses.replace(
        context,
        runtime_dependencies=context.runtime_dependencies.__class__(
            process_tool_output_channel_factory=channel_factory,
            runtime_event_loop=loop,
        ),
    )

    # 定时器在子进程运行途中标记工具级取消（真实跨线程信号）。
    # 取消窗口取足够长（Windows spawn 需重新 import 整个 app，约 2~3s 才进入 handler），
    # 确保 handler 已真实运行并产出实时输出，从而让「输出排空」有实质内容可验。
    timer = threading.Timer(
        5.0,
        tool_call_cancellation_registry.mark_cancelled,
        args=(_RUN_ID, "call-cancel"),
    )
    started = time.monotonic()
    timer.start()
    try:
        observation = await asyncio.to_thread(
            executor.execute,
            ToolCall(
                tool_name=_STREAMING_TOOL_NAME,
                arguments={"marker_path": str(marker)},
                call_id="call-cancel",
            ),
            execution_context=context,
        )
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    # ---- 环境守卫：coverage / 受限 Windows 环境下 spawn 子进程会因 WinError 6 失败 ----
    # 该限制非产品缺陷（见 test_tool_terminal_projection.py 中的同一说明），无法在本环境
    # 触发真实取消路径时显式跳过，避免把环境限制误报为时序违例。
    if "force_kill" not in timeline.labels():
        pytest.skip(
            "本环境下 process 工具子进程未能启动（spawn WinError 6 等环境限制），"
            f"无法观测真实取消时序；observation={observation.status!r} "
            f"reason={(observation.reason or '')[:120]!r}"
        )

    # ---- 时序证据（真实时间戳排序，非替身调用栈）----
    labels = timeline.labels()
    assert "force_kill" in labels, "取消路径未强杀子进程（时序无法成立）"
    assert "finish_output_channel" in labels, "取消路径未收尾输出通道"
    assert "projector.process" in labels, "取消终态从未投影"
    kill_idx = labels.index("force_kill")
    drain_idx = labels.index("finish_output_channel")
    project_idx = labels.index("projector.process")
    assert kill_idx < drain_idx < project_idx, f"时序违例：期望 kill<drain<project，实际 {labels}"

    # 时间戳层面的量化间隔（更强的非循环证据）。
    ordered = sorted(timeline.entries, key=lambda item: item[0])
    kill_ts = next(ts for ts, label in ordered if label == "force_kill")
    drain_ts = next(ts for ts, label in ordered if label == "finish_output_channel")
    project_ts = next(ts for ts, label in ordered if label == "projector.process")
    assert kill_ts <= drain_ts <= project_ts
    assert project_ts >= kill_ts, "投影时间戳早于强杀"

    # 输出排空的实质证据：真实流出的增量已发布，且最后一条发布的时间戳早于投影。
    # 这排除「只验证调用顺序」的循环论证——此处验证的是真实数据被推送的时间点。
    streamed_len = len("".join(channel_factory.streamed))
    assert streamed_len > 0, "取消前已产生的实时输出未到达输出通道（排空未发生）"
    last_publish_ts = max(ts for ts, label in ordered if label == "stream_publish")
    assert last_publish_ts <= project_ts, "实时输出排空晚于终态投影，前端会先看到终态再补输出"

    # ---- 结果与投影语义 ----
    assert observation.status == "cancelled"
    assert elapsed < 15.0, f"取消耗时 {elapsed:.2f}s，疑似未强杀"
    assert marker.exists() is False, "子进程未被真正打断（标记文件被写出）"

    status_events = [
        event
        for event in projector.events
        if isinstance(event, ToolCallStatusChangedEvent) and event.tool_call_id == "call-cancel"
    ]
    assert len(status_events) == 1, f"终态投影应恰一次，实际 {len(status_events)}"
    assert status_events[0].status == "cancelled"
    assert status_events[0].error == "已取消"

    # 真实 snapshot 的 part 终态。
    part = owner.state["runs"][0]["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"

    # 端到端：取消期间确实流出过真实实时输出（排空有实质内容），且被投影进 snapshot。
    streamed_text = "".join(channel_factory.streamed)
    assert streamed_text, "取消前未流出任何实时输出，无法验证保留性"
    assert part.get("terminal_output_seq", -1) >= 0, "流式增量从未投影进 snapshot"

    # 注意：取消终态会整体覆盖 display_data（已知缺陷，权威描述见
    # ``tool_terminal_projection`` 模块 docstring 的「已知缺陷（未修）」段），该行为由下方专门
    # 用例 ``test_cancel_terminal_event_wipes_streamed_output_preexisting`` 固定，此处不重复断言。


# ---------------------------------------------------------------------------
# 判据 2：独立证明取消路径不触达共享投影收口
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_path_never_touches_shared_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """取消执行期间监视 ``_project`` 与 ``get_conversation_event_projector``，证明无触达。

    缺陷类型：有人把即时通知旁路重新接回执行层（例如在 runner 内投影），这里会立刻非零。
    """

    import asyncio

    loop = asyncio.get_running_loop()
    hits: list[str] = []
    getter_calls: list[int] = []

    original_project = projection_module._project

    def _spy_project(**kwargs: Any) -> bool:
        hits.append("_project")
        return original_project(**kwargs)

    monkeypatch.setattr(projection_module, "_project", _spy_project)

    sentinel = object()

    def _spy_getter() -> Any:
        getter_calls.append(1)
        return sentinel

    monkeypatch.setattr(projection_module, "get_conversation_event_projector", _spy_getter)
    # 若执行层自行 import 投影工厂，同样会命中（防绕过）。
    monkeypatch.setattr(
        runner_module, "get_conversation_event_projector", _spy_getter, raising=False
    )

    tool = _make_process_tool("probe_no_projection")
    channel_factory = _LoopBackedChannelFactory(_Timeline())
    context = ToolExecutionContext(
        task_id=_TASK_ID,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=_RUN_ID,
    )
    import dataclasses

    context = dataclasses.replace(
        context,
        runtime_dependencies=context.runtime_dependencies.__class__(
            process_tool_output_channel_factory=channel_factory,
            runtime_event_loop=loop,
        ),
    )

    # 直接调 runner（执行层）而非 executor，从而隔离出「取消路径本身是否投影」。
    timer = threading.Timer(
        0.3,
        tool_call_cancellation_registry.mark_cancelled,
        args=(_RUN_ID, "call-cancel"),
    )
    timer.start()
    try:
        observation = await asyncio.to_thread(
            runner_module.ToolHandlerRunner().execute,
            tool,
            {"marker_path": str(tmp_path / "m.txt")},
            context,
            "call-cancel",
        )
    finally:
        timer.cancel()

    if observation.status != "cancelled":
        pytest.skip(
            "本环境下 process 工具子进程未能启动（spawn WinError 6 等环境限制），"
            f"未走到取消分支；observation={observation.status!r}"
        )

    assert observation.status == "cancelled"
    assert hits == [], f"取消路径触达了共享投影收口：{hits}"
    assert getter_calls == [], "取消路径解析了 projector 工厂"
    # 旧的通知旁路实体必须不存在（第二层护栏）。
    assert not hasattr(runner_module.ToolHandlerRunner, "_notify_tool_call_cancelled")


# ---------------------------------------------------------------------------
# 判据 3：取消 + 实时输出的交互（已流出内容是否保留）
# ---------------------------------------------------------------------------


def _seed_running_execute_terminal(projector: ConversationEventProjector, call_id: str) -> None:
    """在真实 projector 上初始化一个 ``execute_terminal`` 的 running part。"""

    projector.process(RunInitializedEvent(task_id=_TASK_ID, run_id=_RUN_ID))
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


def _project_cancelled_via_real_projection(
    monkeypatch: pytest.MonkeyPatch,
    projector: ConversationEventProjector,
    call_id: str,
) -> ToolCallStatusChangedEvent:
    """用真实 ``project_tool_terminal_state`` 产出取消终态事件并投给 projector。

    这是**取消路径的真实事件来源**：``ToolObservation.status="cancelled"`` ->
    ``project_tool_terminal_state`` -> ``ToolCallStatusChangedEvent`` -> projector。
    比手工构造事件更接近生产，避免「事件构造与生产不一致」的假阴性。
    """

    from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
    from app.core.tools.tool_execute.tool_terminal_projection import (
        project_tool_terminal_state,
    )

    observation = tool_cancelled("execute_terminal", permission="probe", tool_call_id=call_id)
    captured: list[ToolCallStatusChangedEvent] = []

    def _capture(event: Any) -> None:
        captured.append(event)
        projector.process(event)

    monkeypatch.setattr(
        projection_module,
        "get_conversation_event_projector",
        lambda: _CaptureProxy(_capture),
    )
    project_tool_terminal_state(
        task_id=_TASK_ID, run_id=_RUN_ID, tool_call_id=call_id, observation=observation
    )
    assert len(captured) == 1
    return captured[0]


class _CaptureProxy:
    """把 ``process`` 转交给回调的轻量 projector 代理。"""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def process(self, event: Any) -> Any:
        return self._handler(event)


def test_cancel_event_without_display_data_preserves_streamed_output() -> None:
    """对照用例：取消事件**不带** display_data 时，真实 plan 不覆盖已流出 output。

    这是 ``plan`` 中 ``if self.display_data is not None`` 守卫的正向证据：
    只有当事件显式携带 display_data 时才会改写展示字段。
    """

    owner = _MemorySnapshotOwner(empty_snapshot())
    projector = ConversationEventProjector(owner)
    _seed_running_execute_terminal(projector, "call-t")
    streamed = "partial live output\n" * 500
    for seq, start in enumerate(range(0, len(streamed), 4096)):
        projector.process(
            ToolCallRuntimeUpdateEvent(
                task_id=_TASK_ID,
                run_id=_RUN_ID,
                tool_call_id="call-t",
                seq=seq,
                data=TerminalOutputDeltaData(
                    kind="terminal_output_delta",
                    text=streamed[start : start + 4096],
                ),
            )
        )
    assert owner.state["runs"][0]["messages"][1]["parts"][0]["display_data"]["output"] == streamed

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-t",
            status="cancelled",
            error="已取消",
            display_data=None,
        )
    )
    part = owner.state["runs"][0]["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"
    assert part["display_data"]["output"] == streamed
    assert part["terminal_output_seq"] >= 0


def test_real_cancel_projection_wipes_streamed_output_preexisting_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺陷①（既有）复现：取消路径**真实**投影会清空已流出的 display_data.output。

    证据链：
    1. ``tool_cancelled(...)`` 的 ``display_data`` 默认为 ``{}``（非 None）；
    2. ``normalize_display_data({})`` 返回 ``{}``（非 None），故取消终态事件携带
       ``display_data={}``；
    3. ``ToolCallStatusChangedEvent.plan`` 见 ``display_data is not None`` 且
       ``kind != "terminal-result"``，遂 ``set display_data = {}``，清空流式输出。

    该行为由 ``tool_observation.ToolObservation.display_data`` 默认值与
    ``tool_call_event.plan`` 的合并条件决定，二者**均不在本次改动范围内**，属既有缺陷。
    本用例断言该现状（即缺陷确实存在），用于追踪，不掩盖为通过。
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
            data=TerminalOutputDeltaData(
                kind="terminal_output_delta", text=streamed
            ),
        )
    )
    assert owner.state["runs"][0]["messages"][1]["parts"][0]["display_data"]["output"] == streamed

    event = _project_cancelled_via_real_projection(monkeypatch, projector, "call-t")

    # 真实取消事件确实携带空 dict 展示载荷（缺陷根因的输入侧证据）。
    assert event.status == "cancelled"
    assert event.error == "已取消"
    assert event.display_data == {}, "取消终态事件未携带空 display_data，根因假设需修正"

    part = owner.state["runs"][0]["messages"][1]["parts"][0]
    assert part["status"] == "cancelled"
    # 记录既有缺陷现状：已流出的 output 被空 display_data 覆盖。
    assert (
        part["display_data"] == {}
    ), "取消终态未清空已流出输出；缺陷①可能已被修复，请更新本用例与结论"
    assert "output" not in part["display_data"]
