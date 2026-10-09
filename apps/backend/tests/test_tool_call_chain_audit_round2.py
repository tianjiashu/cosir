"""工具调用链路第二轮审计测试（独立测试方，只观测真实行为，不修改生产代码）。

在第一轮 ``test_tool_call_chain_audit.py`` 之外，钉死第二轮交叉审查提出的六个可疑点：

- P1 ``_tools_node`` 重入：checkpoint 停在 ``pending`` 时是否有副作用工具被二次执行
- P2 ``ToolCallStatusChangedEvent`` 终态事件是否清空已流出的终端输出（按 ``kind`` 分形状验证）
- P4 ToolMessage 落库顺序 vs ``AIMessage.tool_calls`` 顺序（内存重排 vs 数据库落库顺序）
- P5 崩溃占位被真实结果覆盖（resume 场景，不并排留第二行）
- P6 ``_observe_node`` 在 run 已取消时是否仍执行一整轮结算

所有用例只依赖内存假对象与 monkeypatch，不连真实模型 / 数据库 / 网络。
"""

from __future__ import annotations

import copy
import unittest.mock as mock
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
import app.core.workflows.react.nodes.observation_node as observation_node_module
import app.core.workflows.react.nodes.tools_node as tools_node_module
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
from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.models.conversation_task_context import ConversationTaskContextRecord

_TASK_ID = 7
_RUN_ID = 9


# ============================================================================
# 共享装配
# ============================================================================


class _FakeStreamWriter:
    """收集 lifecycle 发出的全部事件的假 stream writer。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    @property
    def status_events(self) -> list[ToolCallStatusChangedEvent]:
        return [event for event in self.events if isinstance(event, ToolCallStatusChangedEvent)]


class _LifecycleHarness:
    """lifecycle 的运行时依赖装配：operations / runtime_context / stream writer。"""

    def __init__(
        self,
        registered_tool_names: tuple[str, ...] = ("read_file", "write_file"),
        allows_tools: tuple[str, ...] | None = None,
    ) -> None:
        self.registered_tool_names = tuple(registered_tool_names)
        self.allows_tools = (
            tuple(registered_tool_names) if allows_tools is None else tuple(allows_tools)
        )
        self.writer = _FakeStreamWriter()
        self.messages: list[Any] = []
        self.metadata: list[dict[str, Any]] = []
        self.model_tools = [
            SimpleNamespace(name=name, display=None) for name in self.registered_tool_names
        ]
        self.operations = SimpleNamespace(
            all_vaild_tools=self.model_tools,
            allows_tools=frozenset(self.allows_tools),
            to_tool_model_message=lambda observation: ToolMessage(
                content=str(observation.content or observation.error or ""),
                tool_call_id=observation.tool_call_id,
                name=observation.tool_name,
            ),
            get_current_task=lambda: SimpleNamespace(id=_TASK_ID),
            get_current_run=lambda: SimpleNamespace(id=_RUN_ID),
            is_current_run_cancelled=lambda: self.cancelled,
            fail_run_if_running=lambda **_kwargs: SimpleNamespace(id=_RUN_ID),
        )
        self.cancelled = False
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            run=SimpleNamespace(id=_RUN_ID),
            usage_stats=SimpleNamespace(),
        )

        def _add_message(message: Any, **kwargs: Any) -> str:
            self.messages.append(message)
            self.metadata.append(kwargs.get("transport_metadata") or {})
            return "appended"

        self.runtime_context = SimpleNamespace(add_message=_add_message)
        self.manager = ToolCallLifecycleManager(allows_tools=self.allows_tools)

    @contextmanager
    def runtime(self) -> Iterator[None]:
        """在 lifecycle 模块内注入假运行时依赖（可恢复）。"""

        with mock.patch.multiple(
            lifecycle_module,
            _runtime_config=lambda: self.runtime_config,
            _runtime_context=lambda: self.runtime_context,
            get_stream_writer=lambda: self.writer,
        ):
            yield


def _observation(**overrides: Any) -> ToolObservation:
    """构造最小 ``ToolObservation``（供假 ``run_tool_calls`` 返回）。"""

    base: dict[str, Any] = {
        "tool_name": "read_file",
        "status": "error",
        "content": None,
        "error": "denied",
        "reason": "denied",
        "retryable": False,
        "tool_call_id": "call-1",
        "display_data": {},
    }
    base.update(overrides)
    return ToolObservation(**base)


def _summary(**overrides: Any) -> dict[str, Any]:
    """构造最小观察摘要（``tools`` 节点 ``dataclasses.asdict`` 投影的形状）。"""

    base: dict[str, Any] = {
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "success",
        "error": "",
        "reason": "",
        "content": "ok",
        "retryable": False,
        "display_data": {},
    }
    base.update(overrides)
    return base


def _state(lifecycle: Any, **overrides: Any) -> ReactGraphState:
    """构造节点所需的最小 graph state。"""

    values: dict[str, Any] = {
        "step_count": 3,
        "tool_error_count": 0,
        "next_node": ReactRoute.TOOLS,
        "instruction": "",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {},
        "terminal_sessions": {},
        "tool_call_lifecycle": lifecycle,
    }
    values.update(overrides)
    return ReactGraphState(**values)


class _MemorySnapshotOwner:
    """最小 snapshot owner，按 projector mutations 原地更新测试状态。"""

    def __init__(self, state: ConversationStateSnapshot) -> None:
        self.state = state

    def get_state(self, _task_id: int) -> ConversationStateSnapshot:
        return copy.deepcopy(self.state)

    def apply_planned(self, event: Any) -> TransportFrame:
        mutations = tuple(event.plan(copy.deepcopy(self.state)))
        for mutation in mutations:
            parent: Any = self.state
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


def _seed_tool_part(tool_call_id: str = "call-1") -> tuple[ConversationStateSnapshot, Any]:
    """建立 run + assistant 骨架 + 一个 ``running`` 的 tool-call part，并注入流式输出。"""

    state = empty_snapshot()
    projector = ConversationEventProjector(_MemorySnapshotOwner(state))
    projector.process(RunInitializedEvent(task_id=_TASK_ID, run_id=_RUN_ID))
    projector.process(RunStatusChangedEvent(task_id=_TASK_ID, run_id=_RUN_ID, status="running"))
    projector.process(
        ToolCallCreatedEvent(
            task_id=_TASK_ID, run_id=_RUN_ID, tool_call_id=tool_call_id, tool_name="execute_terminal"
        )
    )
    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID, run_id=_RUN_ID, tool_call_id=tool_call_id, status="running"
        )
    )
    projector.process(
        ToolCallRuntimeUpdateEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id=tool_call_id,
            seq=0,
            data=TerminalOutputDeltaData(kind="terminal_output_delta", text="streamed"),
        )
    )
    return state, projector


def _tool_part(state: ConversationStateSnapshot) -> dict[str, Any]:
    """取 assistant 消息里唯一的 tool-call part。"""

    for message in state["runs"][0]["messages"]:
        for part in message["parts"]:
            if isinstance(part, dict) and part.get("type") == "tool-call":
                return part
    raise AssertionError("tool-call part 不存在")


# ============================================================================
# P1. ``_tools_node`` 重入：pending 记录是否被二次执行
# ============================================================================


async def test_p1_pending_records_are_replayed_on_reentry() -> None:
    """P1-a：checkpoint 停在 ``pending`` 时，有副作用的 ``write_file`` 被再次送去执行。

    被测行为：``_tools_node`` 的可执行集合 = ``valid_tools + blocked_tool_calls`` 中
    ``status == "pending"`` 的记录。工具执行期间进程退出时，checkpoint 仍是 model 节点
    返回的快照（全部 ``pending``），续跑重入本节点会把整批（含 ``write_file`` 这类有副作用
    工具）**重放**。
    缺陷判定：**缺陷**（``tools_node.py`` 顶部 docstring 自述为「剩余缺口（未决）」）。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file", "write_file"),
    )
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file", "write_file"),
        valid_calls={
            "write-1": ToolCallLifecycleRecord(
                tool_call_id="write-1",
                tool_name="write_file",
                status="pending",
                args={"path": "a.txt", "content": "x"},
            ),
        },
    )

    captured: dict[str, Any] = {}

    async def _fake_run_tool_calls(
        task_id: int, calls: list[ToolCall], step_id: str, loop: Any
    ) -> ToolRunResult:
        captured["calls"] = list(calls)
        return ToolRunResult(
            observations=[_observation(tool_name=call.tool_name) for call in calls]
        )

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=_TASK_ID),
            run_tool_calls=_fake_run_tool_calls,
        ),
        run=SimpleNamespace(id=_RUN_ID),
    )

    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        await tools_node_module._tools_node(_state(harness.manager))

    executed = [call.call_id for call in captured["calls"]]
    # 缺陷事实：pending 的 write_file 被再次送去执行（二次写入）。
    assert executed == ["write-1"], f"实际执行集合={executed}"


async def test_p1_running_records_are_not_replayed() -> None:
    """P1-b：对照组——快照已含 ``running``（begin 已写回）时，记录**不再**被送去执行。

    被测行为：``pending_records`` 只收 ``status == "pending"``，``running`` 记录被剔除。
    缺陷判定：符合预期（防重放判据在状态维度有效）。
    注意：真实 LangGraph 语义下 checkpoint 只在节点返回后落盘，因此「工具执行期崩溃」时
    快照恒为 ``pending``，走不到这个分支 —— 本用例只证明过滤逻辑本身有效。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file", "write_file"),
    )
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file", "write_file"),
        valid_calls={
            "write-1": ToolCallLifecycleRecord(
                tool_call_id="write-1", tool_name="write_file", status="running"
            ),
        },
    )

    captured: dict[str, Any] = {}

    async def _fake_run_tool_calls(
        task_id: int, calls: list[ToolCall], step_id: str, loop: Any
    ) -> ToolRunResult:
        captured["calls"] = list(calls)
        return ToolRunResult(observations=[])

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=_TASK_ID),
            run_tool_calls=_fake_run_tool_calls,
        ),
        run=SimpleNamespace(id=_RUN_ID),
    )

    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        await tools_node_module._tools_node(_state(harness.manager))

    assert captured["calls"] == [], f"实际执行集合={captured['calls']}"


async def test_p1_begin_migrates_blocked_calls_without_projecting_event() -> None:
    """P1-c（已修复）：``begin`` 覆盖 ``blocked_calls``，且隐藏调用不发状态事件。

    被测行为：``begin()`` 把 ``valid_calls`` 与 ``blocked_calls`` 中 ``pending`` 的记录一并迁移为
    ``running``，使「未起跑」恒等价于「仍是 ``pending``」——``tools`` 只按该判据选取执行集合，
    因此重入不会把已被门禁拒绝的调用再次送去执行层。``blocked_calls`` 从未建立前端 part
    （``part_projected=False``），迁移不发状态事件，否则 projector 只会记一条「part 缺失」告警。
    缺陷判定：原缺陷（blocked 恒 pending ⇒ 每次重入都被重复送去执行层）已消除。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file",),
        blocked_calls={
            "hidden-1": ToolCallLifecycleRecord(
                tool_call_id="hidden-1",
                tool_name="write_file",
                status="pending",
                part_projected=False,
            ),
        },
    )

    with harness.runtime():
        after_begin = harness.manager.begin(task_id=_TASK_ID, run_id=_RUN_ID, step_id="step-3")

    assert after_begin.blocked_calls["hidden-1"].status == "running"
    assert [event.status for event in harness.writer.status_events] == []


# ============================================================================
# P2. 终态事件是否清空已流出的终端输出（按 display_data.kind 分形状）
# ============================================================================


def test_p2_terminal_result_keeps_streamed_output() -> None:
    """P2-a：``kind == "terminal-result"`` 且新载荷同为 terminal-result 时，输出**被保留**。

    被测行为：``ToolCallStatusChangedEvent.plan`` 的 ``has_streamed_terminal_output`` 补偿分支。
    缺陷判定：符合预期（补偿分支生效）。
    """

    state, projector = _seed_tool_part()
    assert _tool_part(state)["display_data"] == {"kind": "terminal-result", "output": "streamed"}

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="completed",
            display_data={"kind": "terminal-result", "output": "", "exit_code": 0},
        )
    )

    part = _tool_part(state)
    assert part["display_data"]["output"] == "streamed", f"实际={part['display_data']}"
    assert part["display_data"]["exit_code"] == 0


def test_p2_empty_display_data_wipes_streamed_terminal_output() -> None:
    """P2-b：终态事件的 ``display_data`` 为空 ``dict`` 时，已流出的终端输出**被清空**。

    被测行为：取消 / 失败观察的 ``display_data`` 默认是 ``{}`` 而非 ``None``，会通过
    ``if self.display_data is not None`` 守卫整体覆盖 part 的 ``display_data``。
    缺陷判定：**缺陷**（``tool_terminal_projection.py`` 模块 docstring 自述「已知缺陷（未修）」）。
    """

    state, projector = _seed_tool_part()
    assert _tool_part(state)["display_data"]["output"] == "streamed"

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="cancelled",
            display_data={},  # 取消观察的默认形态
        )
    )

    part = _tool_part(state)
    assert part["display_data"] == {}, f"实际={part['display_data']}"
    assert "output" not in part["display_data"], "已流出的输出被清空（缺陷）"


def test_p2_non_terminal_result_kind_is_also_wiped() -> None:
    """P2-c：``kind`` 不是 ``terminal-result`` 时，补偿分支不生效，输出同样被清空。

    被测行为：``has_streamed_terminal_output`` 要求**新旧双方**都是 ``terminal-result``。
    缺陷判定：**缺陷**（补偿只覆盖一种形状）。
    """

    state, projector = _seed_tool_part()

    projector.process(
        ToolCallStatusChangedEvent(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            tool_call_id="call-1",
            status="failed",
            display_data={"kind": "terminal-session", "status_hint": "执行失败"},
        )
    )

    part = _tool_part(state)
    assert part["display_data"] == {
        "kind": "terminal-session",
        "status_hint": "执行失败",
    }, f"实际={part['display_data']}"
    assert "output" not in part["display_data"], "非 terminal-result 形状的输出不被保留（缺陷）"


# ============================================================================
# P4. ToolMessage 顺序 vs AIMessage.tool_calls 顺序
# ============================================================================


def test_p4_settle_batch_writes_tool_messages_in_summary_order() -> None:
    """P4-a：``settle_batch`` 按 summaries 顺序写 ToolMessage，与 ``tool_calls`` 顺序无关。

    被测行为：``model_node`` 把 ``ai_message.tool_calls`` 重写为 ``blocked + valid``，而
    ``settle_batch`` 按 ``observations`` 顺序（valid 先、blocked 后）写 ToolMessage。
    缺陷判定：中等（顺序不一致，但 provider 按 tool_call_id 配对，功能不受影响）。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[
                {"id": "blocked-1", "name": "write_file", "args": {"path": "b.txt"}},
                {"id": "valid-1", "name": "read_file", "args": {"path": "a.txt"}},
            ],
            invalid_tool_calls=[],
        )
        # tool_calls 顺序（model_node 重写后的形态）：blocked 在前。
        assert list(classified.blocked_calls) == ["blocked-1"]
        assert list(classified.valid_calls) == ["valid-1"]

        classified.settle_batch(
            task_id=_TASK_ID,
            run_id=_RUN_ID,
            step_id="step-3",
            # 执行顺序：valid 先、blocked 后（run_tool_calls 的实际顺序）。
            summaries=[
                _summary(tool_call_id="valid-1"),
                _summary(tool_call_id="blocked-1", status="error", error="disabled"),
            ],
            inherited_error_count=0,
        )

    assert [message.tool_call_id for message in harness.messages] == ["valid-1", "blocked-1"]


class _RecordingContextService:
    """记录 append / replace 的最小 context service（不落真实数据库）。"""

    def __init__(self, max_sequence: int = 0) -> None:
        self.max_sequence_value = max_sequence
        self.appended: list[tuple[int, int | None, Any, int, bool]] = []
        self.replaced: list[ConversationTaskContextRecord] = []
        self.loaded: list[ContextEntry] = []
        self.deleted: list[Any] = []

    def max_sequence(self, task_id: int) -> int:  # noqa: D102 - 测试替身
        return self.max_sequence_value

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        return list(self.loaded)

    def get_system_prompt(self, task_id: int) -> ContextEntry | None:
        return None

    def create_system_prompt(self, task_id: int, message: Any) -> ContextEntry:
        return ContextEntry(message, None, -1)

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: Any,
        sequence: int,
        include_in_context: bool,
        transport_metadata: Any = None,
        is_streaming: bool = False,
    ) -> None:
        self.appended.append((task_id, run_id, message, sequence, include_in_context))

    def replace_message(self, record: Any) -> None:  # noqa: D102 - 测试替身
        self.replaced.append(record)

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:  # noqa: D102 - 测试替身
        self.deleted.append(run_id)


def _memory_manager(service: _RecordingContextService, next_sequence: int = 1) -> RuntimeContextManager:
    """构造绕过真实持久化的 ``RuntimeContextManager``（与既有测试同口径）。"""

    manager = object.__new__(RuntimeContextManager)
    manager.current_task_id = _TASK_ID
    manager.agent_profile = SimpleNamespace()
    manager.workspace_root = ""
    manager.compressor = None
    manager.context_service = service
    manager.current_run_id = _RUN_ID
    manager.is_fork = False
    manager._system_entry = ContextEntry(SystemMessage(content="system"), None, -1)
    manager._entries = []
    manager._message_sequence = next_sequence
    manager._streaming_messages = {}
    return manager


def test_p4_memory_reorders_but_persisted_order_is_never_fixed() -> None:
    """P4-b：``load_message`` 只在**内存**里重排 ToolMessage，数据库落库顺序永不修正。

    被测行为：AI(tool_calls=[blocked-1, valid-1]) 之后先落 valid-1、再落 blocked-1；
    ``_close_unclosed_tool_calls`` 把内存工作副本重排为 tool_calls 顺序，但
    ``context_service`` 只收到 append，没有任何改写顺序的调用。
    缺陷判定：低危（provider 按 id 配对；仅库内 / 冷读顺序与 tool_calls 顺序不一致）。
    """

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)

    manager.add_message(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "write_file", "args": {}, "id": "blocked-1"},
                {"name": "read_file", "args": {}, "id": "valid-1"},
            ],
        )
    )
    # 落库顺序故意与 tool_calls 相反（valid 先、blocked 后）。
    manager.add_message(ToolMessage(content="valid result", tool_call_id="valid-1"))
    manager.add_message(ToolMessage(content="disabled", tool_call_id="blocked-1"))

    persisted_before = [
        getattr(message, "tool_call_id", None) for _, _, message, _, _ in service.appended
    ]
    assert persisted_before == [None, "valid-1", "blocked-1"], f"落库顺序={persisted_before}"

    loaded = manager.load_message()
    tool_messages = [
        message.tool_call_id for message in loaded if isinstance(message, ToolMessage)
    ]
    # 内存工作副本被重排为 tool_calls 顺序。
    assert tool_messages == ["blocked-1", "valid-1"], f"内存顺序={tool_messages}"
    # 数据库侧没有任何改写：append 记录不变，replace 为空。
    persisted_after = [
        getattr(message, "tool_call_id", None) for _, _, message, _, _ in service.appended
    ]
    assert persisted_after == persisted_before, "数据库落库顺序未被修正（缺陷：永不修正）"
    assert service.replaced == []


# ============================================================================
# P5. 崩溃占位被真实结果覆盖（resume 场景）
# ============================================================================


def test_p5_placeholder_is_replaced_by_real_result_after_resume() -> None:
    """P5-a：B 崩溃 → 补 ``cancelled`` 占位 → 真实结果到达时**原地覆盖**，不并排留第二行。

    被测行为：占位写入时携带 ``run_id=plan.target_run_id``，与 resume 后的
    ``current_run_id`` 相同，因此 ``add_message`` 的 ``candidate.run_id == target_run_id``
    判定成立，走 ``replace_message`` 分支。
    缺陷判定：符合预期（不是缺陷）。
    """

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)

    manager.add_message(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "args": {}, "id": "A"},
                {"name": "write_file", "args": {}, "id": "B"},
            ],
        )
    )
    manager.add_message(ToolMessage(content="A result", tool_call_id="A"))

    # 模拟进程崩溃后 resume：load_message 为未配对的 B 补 cancelled 占位。
    loaded = manager.load_message()
    tool_ids = [message.tool_call_id for message in loaded if isinstance(message, ToolMessage)]
    assert tool_ids == ["A", "B"], f"补占位后的顺序={tool_ids}"
    assert "did not produce a result" in str(loaded[-1].content), "B 应得到 cancelled 占位"

    # 真实结果到达。
    outcome = manager.add_message(
        ToolMessage(content="real B result", tool_call_id="B"),
        transport_metadata={"status": "completed"},
    )

    assert outcome == "replaced", "占位必须被原地覆盖，而不是并排追加第二行"
    assert len(service.replaced) == 1
    assert [entry.message.content for entry in manager._entries] == ["", "A result", "real B result"]
    # 总行数没有增加：只有 AI / A / B 三行（B 那行被原地替换）。
    assert len(manager._entries) == 3


def test_p5_placeholder_of_another_run_is_not_overwritten() -> None:
    """P5-b：对照组——归属不同 Run 的既有结果行不会被覆盖（跨 run 保护生效）。

    被测行为：``add_message(run_id=<另一个 run>)`` 时 ``candidate.run_id != target_run_id``，
    走 append 分支。缺陷判定：符合预期。
    """

    service = _RecordingContextService(max_sequence=0)
    manager = _memory_manager(service, next_sequence=1)
    manager.current_run_id = _RUN_ID

    manager.add_message(
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "A"}])
    )
    manager.add_message(ToolMessage(content="old run result", tool_call_id="A"))

    outcome = manager.add_message(
        ToolMessage(content="new run result", tool_call_id="A"), run_id=_RUN_ID + 1
    )

    assert outcome == "appended", "不同 Run 的结果行不得被覆盖"
    assert len(manager._entries) == 3


# ============================================================================
# P6. ``_observe_node`` 在 run 已取消时是否仍执行一整轮结算
# ============================================================================


async def test_p6_observe_node_settles_even_when_run_is_cancelled() -> None:
    """P6：run 已被取消时，``_observe_node`` **仍然**结算整批观察并路由回 model。

    被测行为：``_observe_node`` 全文没有 ``is_current_run_cancelled()`` 检查。
    缺陷判定：低危（收敛仍可靠：路由回 model 后由 ``model_node`` 入口检查命中并 interrupt；
    代价是被取消的 run 会多跑一轮结算）。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file",),
        allows_tools=("read_file",),
    )
    harness.cancelled = True

    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[
                {"id": "ok-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "cancelled-1", "name": "read_file", "args": {"path": "b.txt"}},
            ],
            invalid_tool_calls=[],
        )

    state = _state(
        classified,
        last_tool_results={
            "instruction": "",
            "observations": [
                _summary(tool_call_id="ok-1"),
                _summary(tool_call_id="cancelled-1", status="cancelled"),
            ],
        },
        next_node=ReactRoute.MODEL,
    )

    with harness.runtime(), mock.patch.object(
        observation_node_module, "_runtime_config", lambda: harness.runtime_config
    ):
        patch = await observation_node_module._observe_node(state)

    # 事实 1：两条观察都被结算（写了 2 条 ToolMessage），尽管 run 已取消。
    assert [message.tool_call_id for message in harness.messages] == ["ok-1", "cancelled-1"]
    # 事实 2：终态事件照发（ok-1 completed / cancelled-1 cancelled）。
    assert [(event.tool_call_id, event.status) for event in harness.writer.status_events] == [
        ("ok-1", "completed"),
        ("cancelled-1", "cancelled"),
    ]
    # 事实 3：路由回 model，由 model_node 入口的取消检查收敛。
    assert patch["next_node"] == ReactRoute.MODEL
    # 事实 4：取消不计入连续失败。
    assert patch["tool_error_count"] == 0
