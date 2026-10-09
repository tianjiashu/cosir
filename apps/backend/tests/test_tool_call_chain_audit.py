"""工具调用链路审计测试（独立测试方，只观测真实行为，不修改生产代码）。

按 T1..T4 四组钉死当前实现的真实行为：

- T1 ``tools_node._tools_node`` 重入重放（终态记录是否仍被送去执行 + ``begin`` 返回值是否写回 patch）
- T2 ``ToolAccessGate.evaluate`` 的检查顺序（本轮禁用 ∩ 参数非法 时返回哪种 denial）
- T3 ``ToolCallLifecycleManager.settle`` 对「工具名未注册」记录是否发终态事件（对照 ``cancel``）
- T4 冷重建 ``build_pair_tool_part`` 是否为「本轮被隐藏闭合」的调用也建 part

所有用例只依赖内存假对象与 monkeypatch，不连真实模型 / 数据库 / 网络。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import app.core.tools.tool_execute.tool_access_gate as gate_module
import app.core.workflows.react.node_helper.tool_call_lifecycle as lifecycle_module
import app.core.workflows.react.nodes.tools_node as tools_node_module
from app.assistant_transport.event import (
    ToolCallCreatedEvent,
    ToolCallStatusChangedEvent,
)
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.hook.hook_event import HookDecision
from app.core.hook.hook_result import HookResult
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import (
    ToolCall,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_access_gate import ToolAccessGate
from app.core.tools.tool_handler.read_file import ReadFileTool
from app.core.tools.tool_registry import ToolRegistry
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.models.conversation_task_context import ConversationTaskContextRecord

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

    @property
    def created_events(self) -> list[ToolCallCreatedEvent]:
        return [event for event in self.events if isinstance(event, ToolCallCreatedEvent)]


class _LifecycleHarness:
    """lifecycle 的运行时依赖装配：operations / runtime_context / stream writer。"""

    def __init__(
        self,
        registered_tool_names: tuple[str, ...] = ("read_file",),
        allows_tools: tuple[str, ...] | None = None,
    ) -> None:
        self.registered_tool_names = tuple(registered_tool_names)
        self.allows_tools = (
            tuple(registered_tool_names) if allows_tools is None else tuple(allows_tools)
        )
        self.writer = _FakeStreamWriter()
        self.messages: list[Any] = []
        # 与 messages 一一对应的 Transport metadata（用于断言隐藏闭合的 ``hidden`` 标记）。
        self.metadata: list[dict[str, Any]] = []
        self.model_tools = [
            SimpleNamespace(name=name, display=None) for name in self.registered_tool_names
        ]
        self.operations = SimpleNamespace(
            all_vaild_tools=self.model_tools,
            model_tools=self.model_tools,
            allows_tools=frozenset(self.allows_tools),
            to_tool_model_message=lambda observation: ToolMessage(
                content=str(observation.content or observation.error or ""),
                tool_call_id=observation.tool_call_id,
                name=observation.tool_name,
            ),
        )
        self.runtime_config = SimpleNamespace(
            operations=self.operations,
            run=SimpleNamespace(id=200),
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

        import unittest.mock as mock

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
        "error": "replayed",
        "reason": "replayed",
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


def _state(lifecycle: ToolCallLifecycleManager, **overrides: Any) -> ReactGraphState:
    """构造 ``_tools_node`` 所需的最小 graph state。"""

    values: dict[str, Any] = {
        "step_count": 3,
        "tool_error_count": 0,
        "next_node": ReactRoute.TOOLS,
        "instruction": "请读取文件",
        "max_steps": 10,
        "final_text": "",
        "last_tool_results": {},
        "terminal_sessions": {},
        "tool_call_lifecycle": lifecycle,
    }
    values.update(overrides)
    return ReactGraphState(**values)


def _execution_context(tmp_path: Any) -> ToolExecutionContext:
    """构造门禁所需的最小执行边界（不触碰真实文件系统）。"""

    return ToolExecutionContext(
        task_id=11,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=200,
        tool_call_id="call-1",
    )


# ============================================================================
# T1. ``_tools_node`` 重入重放
# ============================================================================


async def test_t1_terminal_records_are_not_sent_to_execution_on_reentry() -> None:
    """T1-a：lifecycle 中已终态 / 已起跑的记录在 ``_tools_node`` 重入时**不再**被送去执行。

    被测行为：``_tools_node`` 的可执行集合 = ``valid_tools + blocked_tool_calls`` 中
    ``status == "pending"`` 的记录（修复前不过滤，5 条全部被重放）。
    修复后实际观测：completed / failed / cancelled 三条终态记录被剔除，只有 pending 的
    ``fresh-pending`` 与隐藏闭合的 ``hidden-blocked`` 被送执行。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file",),
        valid_calls={
            "settled-completed": ToolCallLifecycleRecord(
                tool_call_id="settled-completed",
                tool_name="read_file",
                status="completed",
                args={"path": "a.txt"},
            ),
            "settled-failed": ToolCallLifecycleRecord(
                tool_call_id="settled-failed",
                tool_name="read_file",
                status="failed",
                args={"path": "b.txt"},
            ),
            "settled-cancelled": ToolCallLifecycleRecord(
                tool_call_id="settled-cancelled",
                tool_name="read_file",
                status="cancelled",
                args={"path": "c.txt"},
            ),
            "fresh-pending": ToolCallLifecycleRecord(
                tool_call_id="fresh-pending",
                tool_name="read_file",
                status="pending",
                args={"path": "d.txt"},
            ),
        },
        blocked_calls={
            "hidden-blocked": ToolCallLifecycleRecord(
                tool_call_id="hidden-blocked",
                tool_name="write_file",
                status="pending",
                args={"path": "e.txt"},
            ),
        },
    )

    captured: dict[str, Any] = {}

    async def _fake_run_tool_calls(task_id: int, calls: list[ToolCall], step_id: str, loop: Any):
        captured["task_id"] = task_id
        captured["step_id"] = step_id
        captured["calls"] = list(calls)
        return ToolRunResult(
            observations=[
                _observation(tool_name=call.tool_name, tool_call_id=call.call_id)
                for call in calls
            ]
        )

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=11),
            run_tool_calls=_fake_run_tool_calls,
        ),
        run=SimpleNamespace(id=200),
    )

    import unittest.mock as mock

    state = _state(harness.manager)
    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        patch = await tools_node_module._tools_node(state)

    executed_ids = [call.call_id for call in captured["calls"]]

    # 事实 1：已终态记录（completed / failed / cancelled）被剔除，不再重放。
    assert "settled-completed" not in executed_ids, f"实际执行集合={executed_ids}"
    assert "settled-failed" not in executed_ids, f"实际执行集合={executed_ids}"
    assert "settled-cancelled" not in executed_ids, f"实际执行集合={executed_ids}"
    # 事实 2：blocked_calls（本轮禁用、隐藏闭合）仍被送去执行层（由门禁拒绝以闭合协议）。
    assert "hidden-blocked" in executed_ids, f"实际执行集合={executed_ids}"
    assert "fresh-pending" in executed_ids, f"实际执行集合={executed_ids}"
    assert len(executed_ids) == 2, f"实际执行条数={len(executed_ids)}"
    assert captured["task_id"] == 11
    assert captured["step_id"] == "step-3"
    # 事实 3：节点把执行结果原样投影进 last_tool_results。
    assert len(patch["last_tool_results"]["observations"]) == 2


async def test_t1_begin_snapshot_is_written_back_to_state_patch() -> None:
    """T1-b：``lifecycle.begin()`` 返回的 ``running`` 快照被 tools_node 写回 state patch。

    被测行为：``_tools_node`` 接住 ``begin`` 的返回值，并把它放进返回 patch 的
    ``tool_call_lifecycle`` 键（修复前返回值被丢弃，patch 只有两个键）。
    修复后实际观测：patch 键含 ``tool_call_lifecycle``，其中已起跑的记录为 ``running``；
    state 内的**原**快照不被就地改写（begin 是 copy-on-write），仍停在 ``pending``。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file",),
        valid_calls={
            "fresh-pending": ToolCallLifecycleRecord(
                tool_call_id="fresh-pending",
                tool_name="read_file",
                status="pending",
                args={"path": "d.txt"},
            ),
        },
    )

    async def _fake_run_tool_calls(task_id: int, calls: list[ToolCall], step_id: str, loop: Any):
        return ToolRunResult(
            observations=[
                _observation(tool_name=call.tool_name, tool_call_id=call.call_id)
                for call in calls
            ]
        )

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=11),
            run_tool_calls=_fake_run_tool_calls,
        ),
        run=SimpleNamespace(id=200),
    )

    import unittest.mock as mock

    state = _state(harness.manager)
    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        # 先直接观测 begin 的返回值：pending → running，并发一条 running 事件。
        begin_result = harness.manager.begin(task_id=11, run_id=200, step_id="step-3")
        events_after_manual_begin = [
            (event.tool_call_id, event.status) for event in harness.writer.status_events
        ]
        patch = await tools_node_module._tools_node(state)

    # 事实 A：begin 的返回值里该记录已迁移为 running，并发出一条 running 事件。
    assert begin_result.valid_calls["fresh-pending"].status == "running"
    assert events_after_manual_begin == [("fresh-pending", "running")]
    # 事实 B：_tools_node 内部对（仍停在 pending 的）同一份快照又跑了一次 begin → 第二条 running。
    assert [(event.tool_call_id, event.status) for event in harness.writer.status_events] == [
        ("fresh-pending", "running"),
        ("fresh-pending", "running"),
    ]

    # 事实：返回的 patch 写入了 begin 后的快照，已起跑的记录为 running。工具节点不参与用户审批，
    # 因此 patch 里没有待决请求字段（请求由观察上的声明派生，见 wait_user_node）。
    assert set(patch) == {
        "last_tool_results",
        "terminal_sessions",
        "tool_call_lifecycle",
    }, f"patch 键={sorted(patch)}"
    written_back = patch["tool_call_lifecycle"]
    assert written_back.valid_calls["fresh-pending"].status == "running"
    # 事实：state 内的原快照未被就地改写，pending 记录仍停在 pending。
    assert state.tool_call_lifecycle is not None
    assert state.tool_call_lifecycle.valid_calls["fresh-pending"].status == "pending"


async def test_t1_missing_lifecycle_raises_runtime_error() -> None:
    """T1-c：``tool_call_lifecycle`` 缺失时 ``_tools_node`` 直接抛 RuntimeError（契约边界）。

    被测行为：state 未携带 lifecycle 时节点不做静默降级。
    实际观测：抛 ``RuntimeError("tool_call_lifecycle is required before tools_node execution")``。
    是否符合预期：符合（节点前置契约显式校验）。
    """

    harness = _LifecycleHarness()
    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=11),
            run_tool_calls=None,
        ),
        run=SimpleNamespace(id=200),
    )

    import unittest.mock as mock

    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        with pytest.raises(RuntimeError, match="tool_call_lifecycle is required"):
            await tools_node_module._tools_node(_state(None))


async def test_t1_terminal_session_display_data_is_projected_into_checkpoint() -> None:
    """T1-d：终端会话展示元数据只按 allowlist 投影进 ``terminal_sessions``。

    实际观测：``display_data.kind == "terminal-session"`` 的观察把 allowlisted 字段
    （session_id / status / exit_code …）并入 checkpoint；``display_data`` 里的原始
    output / 非 allowlist 字段被丢弃；非 terminal-session 的展示数据完全不投影。
    是否符合预期：符合（投影只承载可序列化展示元数据，避免把进程对象落 checkpoint）。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    harness.manager = ToolCallLifecycleManager(
        allows_tools=("read_file",),
        valid_calls={
            "term-1": ToolCallLifecycleRecord(
                tool_call_id="term-1", tool_name="read_file", status="pending"
            ),
        },
    )

    async def _fake_run_tool_calls(task_id: int, calls: list[ToolCall], step_id: str, loop: Any):
        return ToolRunResult(
            observations=[
                _observation(
                    tool_call_id="term-1",
                    status="success",
                    content="out",
                    display_data={
                        "kind": "terminal-session",
                        "session_id": "sess-9",
                        "status": "exited",
                        "exit_code": 3,
                        "output": "SHOULD-NOT-BE-PROJECTED",
                    },
                ),
                _observation(
                    tool_call_id="plain-1",
                    display_data={"kind": "file-list", "files": ["a.py"]},
                ),
            ]
        )

    runtime_config = SimpleNamespace(
        operations=SimpleNamespace(
            get_current_task=lambda: SimpleNamespace(id=11),
            run_tool_calls=_fake_run_tool_calls,
        ),
        run=SimpleNamespace(id=200),
    )

    import unittest.mock as mock

    state = _state(harness.manager, terminal_sessions={"sess-1": {"session_id": "sess-1"}})
    with harness.runtime(), mock.patch.object(
        tools_node_module, "_runtime_config", lambda: runtime_config
    ):
        patch = await tools_node_module._tools_node(state)

    sessions = patch["terminal_sessions"]
    assert set(sessions) == {"sess-1", "sess-9"}
    assert sessions["sess-9"]["status"] == "exited"
    assert sessions["sess-9"]["exit_code"] == 3
    assert "output" not in sessions["sess-9"], f"落库字段={sorted(sessions['sess-9'])}"
    assert "plain-1" not in sessions


# ============================================================================
# T2. ``ToolAccessGate.evaluate`` 检查顺序
# ============================================================================


def test_t2_disabled_and_invalid_args_yields_argument_denial(tmp_path: Any) -> None:
    """T2-a：「已注册但不在 allowed_tool_names」∩「参数非法」→ 必须命中**权限门禁** denial。

    被测行为：``evaluate`` 的四段检查实际执行顺序（注册表 → 权限门禁 → 参数校验 → Hook）。
    修复前实际观测（缺陷）：denial.error 以 ``"invalid tool arguments: "`` 开头、
    ``retryable is True`` —— 参数校验排在权限门禁之前，被禁用的工具会向模型回传
    「改改参数就能重试」的错误信号。
    修复后（当前断言）：权限门禁先于参数校验，参数是否合法都不影响命中「本轮禁用」分支。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="read_file", arguments={"nonexistent_field": 1}, call_id="call-disabled"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"other_tool"},  # read_file 已注册但本轮被禁用
    )

    assert outcome.admitted is False
    assert outcome.tool is None
    assert outcome.denial is not None
    # 关键事实：命中的是「本轮禁用」分支，而不是「参数非法」分支。
    assert outcome.denial.error == "tool is not allowed: read_file", outcome.denial.error
    assert outcome.denial.retryable is False
    assert "disabled for the current run" in (outcome.denial.reason or "")
    assert outcome.denial.tool_call_id == "call-disabled"


def test_t2_disabled_with_valid_args_yields_disabled_denial(tmp_path: Any) -> None:
    """T2-b：对照用例——参数合法 + 本轮被禁用 → 同一个「本轮禁用」denial（retryable=False）。

    与 T2-a 断言完全同值，才说明「是否被禁用」不再取决于参数是否恰好合法（修复前 T2-a 与
    T2-b 会命中两个不同分支，同一条禁用策略对模型的呈现不稳定）。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="read_file", arguments={"path": "a.txt"}, call_id="call-disabled-2"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"other_tool"},
    )

    assert outcome.admitted is False
    assert outcome.denial is not None
    assert outcome.denial.error == "tool is not allowed: read_file"
    assert "disabled for the current run" in (outcome.denial.reason or "")
    assert outcome.denial.retryable is False


def test_t2_unknown_tool_wins_over_everything(tmp_path: Any) -> None:
    """T2-c：未注册工具名 = 最优先拒绝（unknown tool / retryable=False），不看 allowed 集合。

    实际观测：error 以 ``"unknown tool: "`` 开头，retryable is False，reason 提示「同名字永远被拒」。
    是否符合预期：符合（注册表命中是最前置检查，与 docstring 一致）。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="ghost_tool", arguments={}, call_id="call-ghost"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names=set(),  # 连空白名单都要先让位给「注册表未命中」
    )

    assert outcome.admitted is False
    assert outcome.denial is not None
    assert outcome.denial.error.startswith("unknown tool: ")
    assert outcome.denial.retryable is False
    assert "always be rejected" in (outcome.denial.reason or "")


def test_t2_pre_tool_use_hook_deny_and_argument_rewrite(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2-d：PreToolUse Hook 是最后一段检查——DENY 硬拒绝、modified_arguments 改写入参。

    实际观测：
    - DENY：``admitted=False``，denial.error == deny_reason，且 ``tool`` 字段为 None；
    - 改写：``admitted=True``，``outcome.arguments`` **整体替换**为 Hook 返回的字典
      （实测 ``{"path": "z.txt"}``），既不与校验后的参数合并，也不补 args_model 的默认值
      （``offset`` / ``limit`` 由此丢失）；``call.arguments`` 本身不被回写。
    是否符合预期：符合（Hook 是门禁的最后一段，改写参数在执行前生效）；但「整体替换而非合并」
    会让部分改写的 Hook 静默抹掉必填/默认参数，属需要留意的边界。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)

    class _DenyHook:
        @staticmethod
        def safe_fire(_context: Any) -> HookResult:
            return HookResult(decision=HookDecision.DENY, deny_reason="blocked by policy")

    monkeypatch.setattr(gate_module, "HookInterceptor", _DenyHook)
    denied = gate.evaluate(
        ToolCall(tool_name="read_file", arguments={"path": "a.txt"}, call_id="call-hook"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"read_file"},
    )
    assert denied.admitted is False
    assert denied.tool is None
    assert denied.denial is not None
    assert denied.denial.error == "blocked by policy"
    assert denied.denial.tool_call_id == "call-hook"

    class _RewriteHook:
        @staticmethod
        def safe_fire(_context: Any) -> HookResult:
            return HookResult(decision=HookDecision.ALLOW, modified_arguments={"path": "z.txt"})

    monkeypatch.setattr(gate_module, "HookInterceptor", _RewriteHook)
    rewritten = gate.evaluate(
        ToolCall(tool_name="read_file", arguments={"path": "a.txt"}, call_id="call-hook-2"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"read_file"},
    )
    assert rewritten.admitted is True
    assert rewritten.arguments == {"path": "z.txt"}


# ============================================================================
# T3. ``settle`` 对「工具名未注册」记录是否发终态事件
# ============================================================================


def test_t3_settle_closes_unregistered_tool_name_record_silently() -> None:
    """T3-a：``settle`` 对「工具名未注册」的记录**静默闭合**（不发终态事件），与 ``cancel`` 同口径。

    被测行为：先用 ``classify`` 把未注册工具名（``ghost_tool``）造进 ``valid_calls``
    （其 ``part_projected`` 为 ``False``），再对同一 call_id 结算一条 success 观察。
    修复后实际观测：**不发** ``ToolCallStatusChangedEvent``，只写一条 ``ToolMessage``，
    且该 ToolMessage 的 ``transport_metadata`` 带 ``hidden=True``（供冷重建跳过建 part）。
    修复前：会向一个前端不存在的 part 发终态事件（projector 记「part 缺失」告警）。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[{"id": "ghost-1", "name": "ghost_tool", "args": {}}],
            invalid_tool_calls=[],
        )

    # 前置事实：未注册名进 valid_calls，且未发任何创建事件、未投影为 part。
    assert "ghost-1" in classified.valid_calls
    assert classified.valid_calls["ghost-1"].tool_name == "ghost_tool"
    assert classified.valid_calls["ghost-1"].part_projected is False
    assert harness.writer.created_events == []

    with harness.runtime():
        updated, event_status = classified.settle(
            task_id=11,
            run_id=200,
            step_id="step-3",
            summary=_summary(tool_call_id="ghost-1", tool_name="ghost_tool"),
        )

    assert event_status == "completed"
    assert harness.writer.status_events == [], f"实发事件={harness.writer.status_events}"
    assert updated.valid_calls["ghost-1"].status == "completed"
    # 模型侧协议照常闭合（写一条 ToolMessage），并带隐藏标记。
    assert [message.tool_call_id for message in harness.messages] == ["ghost-1"]
    assert harness.metadata[-1].get("hidden") is True


def test_t3_cancel_skips_the_same_unregistered_record() -> None:
    """T3-b：对照用例——``cancel`` 对同一条「未注册工具名」记录**跳过**、不发任何事件。

    实际观测：``cancel()`` 后事件列表为空，记录状态仍为 ``pending``（未被收口）。
    修复后 ``settle``（T3-a）与 ``cancel`` 共用 ``part_projected`` 判据，两条路径同口径。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[{"id": "ghost-1", "name": "ghost_tool", "args": {}}],
            invalid_tool_calls=[],
        )
        cancelled = classified.cancel(task_id=11, run_id=200, step_id="step-3")

    assert harness.writer.events == []
    assert cancelled.valid_calls["ghost-1"].status == "pending"


def test_t3_settle_closes_orphan_record_silently() -> None:
    """T3-c：``settle`` 对**完全没有记录**的 call_id 即时补建 pending 记录并**静默闭合**。

    修复后实际观测：补建记录的 ``part_projected`` 恒为 ``False``，因此不发终态事件，只写
    ToolMessage 并标记 ``hidden``。修复前：补建路径无条件发终态事件（孤儿调用同样没有 part）。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    with harness.runtime():
        updated, event_status = harness.manager.settle(
            task_id=11,
            run_id=200,
            step_id="step-3",
            summary=_summary(tool_call_id="orphan-1", tool_name="ghost_tool"),
        )

    assert event_status == "completed"
    assert updated.valid_calls["orphan-1"].status == "completed"
    assert updated.valid_calls["orphan-1"].part_projected is False
    assert harness.writer.status_events == [], f"实发事件={harness.writer.status_events}"
    assert harness.metadata[-1].get("hidden") is True


def test_t3_blocked_call_is_closed_silently_by_settle() -> None:
    """T3-d：对照组——``blocked_calls``（已注册但本轮禁用）被 settle 静默闭合（只写 ToolMessage）。

    实际观测：写入一条 ``ToolMessage``，**不**发终态事件。
    是否符合预期：符合（隐藏闭合：前端无 part，发事件只会让 projector 记「part 缺失」告警）。
    本用例证明「静默闭合」是设计内的既有能力，从而坐实 T3-a 的未注册名分支是遗漏而非取舍。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[{"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}}],
            invalid_tool_calls=[],
        )

    assert "hidden-1" in classified.blocked_calls

    with harness.runtime():
        updated, event_status = classified.settle(
            task_id=11,
            run_id=200,
            step_id="step-3",
            summary=_summary(
                tool_call_id="hidden-1",
                tool_name="write_file",
                status="error",
                error="This tool is disabled for the current run.Do not call again",
                retryable=False,
            ),
        )

    assert event_status == "failed"
    assert harness.writer.status_events == []
    assert [message.tool_call_id for message in harness.messages] == ["hidden-1"]
    assert updated.blocked_calls["hidden-1"].status == "failed"


def test_t3_settle_is_idempotent_for_already_terminal_records() -> None:
    """T3-e：已终态记录再次 settle 时提前返回，不重复发事件、不重复写 ToolMessage。

    实际观测：第二次 settle 后事件数与消息数均保持 1。
    是否符合预期：符合（幂等兜底，避免 checkpoint 重放时重复投影）。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[{"id": "call-1", "name": "read_file", "args": {"path": "a.txt"}}],
            invalid_tool_calls=[],
        )
        first, _ = classified.settle(
            task_id=11, run_id=200, step_id="step-3", summary=_summary()
        )
        second, _ = first.settle(
            task_id=11, run_id=200, step_id="step-3", summary=_summary()
        )

    assert len(harness.writer.status_events) == 1
    assert len(harness.messages) == 1
    assert second.valid_calls["call-1"].status == "completed"


def test_t3_cancel_settles_registered_allowed_call_but_skips_blocked() -> None:
    """T3-f：``cancel`` 只对「已注册 ∩ 本轮放行 ∩ 未终态」的记录发 cancelled 事件。

    实际观测：
    - ``read_file``（已注册且在 allows_tools）→ 发一条 cancelled 事件，记录变为 cancelled；
    - ``write_file``（已注册但不在 allows_tools，落在 blocked_calls）→ 完全不参与收口，
      状态保持 pending，无事件。
    是否符合预期：符合（blocked_calls 从未投影为 part，收口没有对象）。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[
                {"id": "ok-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "blocked-1", "name": "write_file", "args": {"path": "b.txt"}},
            ],
            invalid_tool_calls=[],
        )
        cancelled = classified.cancel(task_id=11, run_id=200, step_id="step-3")

    assert [(event.tool_call_id, event.status) for event in harness.writer.status_events] == [
        ("ok-1", "cancelled")
    ]
    assert cancelled.valid_calls["ok-1"].status == "cancelled"
    assert cancelled.blocked_calls["blocked-1"].status == "pending"


def test_t3_create_projects_only_allowed_bucket() -> None:
    """T3-g：``create`` 只为 allowed 桶发创建事件并写展示声明；blocked / 未注册名只落 state。

    实际观测：只有 ``read_file`` 发了一条 ``ToolCallCreatedEvent``；``write_file`` 进
    ``blocked_calls``、``ghost_tool`` 进 ``valid_calls``，两者都没有创建事件也没有展示声明。
    是否符合预期：符合（隐藏闭合集合刻意不投影到前端）。
    """

    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        created = harness.manager.create(
            task_id=11,
            run_id=200,
            step_id="step-3",
            raw_tool_calls=[
                {"id": "ok-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "blocked-1", "name": "write_file", "args": {"path": "b.txt"}},
                {"id": "ghost-1", "name": "ghost_tool", "args": "{'broken':"},
            ],
        )

    assert [event.tool_call_id for event in harness.writer.created_events] == ["ok-1"]
    assert "blocked-1" in created.blocked_calls
    assert "ghost-1" in created.valid_calls
    # 未闭合 JSON 片段（str）被放进保留键，而不是被抹成空参数。
    assert created.valid_calls["ghost-1"].args == {"Invaild_args": "{'broken':"}


def test_t3_settle_batch_error_counting_and_lifecycle_writeback() -> None:
    """T3-h：``settle_batch`` 的连续失败计数与 lifecycle 写回契约。

    实际观测：
    - failed 且 ``retryable is False`` → ``tool_error_count`` 与 ``error_count`` 各 +1；
    - cancelled / 可重试失败 → 既不计也不清零；
    - completed → 清零；
    - 返回值里的 ``lifecycle`` 非空，供 observe 节点写回 state。
    是否符合预期：符合（observe 节点的错误上限判定依赖该计数）。
    """

    harness = _LifecycleHarness(registered_tool_names=("read_file",))
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "a.txt"}}],
            invalid_tool_calls=[],
        )
        result = classified.settle_batch(
            task_id=11,
            run_id=200,
            step_id="step-3",
            summaries=[
                _summary(tool_call_id="c1", status="error", error="boom", retryable=False),
                _summary(tool_call_id="c2", status="error", error="again", retryable=True),
                _summary(tool_call_id="c3", status="cancelled"),
            ],
            inherited_error_count=2,
        )

    assert result.tool_error_count == 3
    assert result.error_count == 1
    assert result.lifecycle is not None
    assert set(result.lifecycle.valid_calls) == {"c1", "c2", "c3"}


# ============================================================================
# T4. 冷重建 vs 实时投影
# ============================================================================


class _FakeRebuildRegistry:
    """冷重建用的假工具注册表：未注册名返回 None，已注册名无 display 声明。"""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._names = set(names)

    def get_tool_definition(self, name: str) -> Any:
        if name not in self._names:
            return None
        return SimpleNamespace(display=None)


def _ai_row(tool_calls: list[dict[str, Any]]) -> ConversationTaskContextRecord:
    return ConversationTaskContextRecord(
        id=1,
        task_id=7,
        run_id=1,
        message=AIMessage(content="", tool_calls=tool_calls),
        include_in_context=False,
        sequence=1,
    )


def _tool_row(
    row_id: int,
    tool_call_id: str,
    content: str,
    metadata: dict[str, Any],
    sequence: int,
) -> ConversationTaskContextRecord:
    return ConversationTaskContextRecord(
        id=row_id,
        task_id=7,
        run_id=1,
        message=ToolMessage(content=content, tool_call_id=tool_call_id),
        include_in_context=False,
        sequence=sequence,
        transport_metadata=metadata,
    )


def test_t4_cold_rebuild_skips_the_hidden_disabled_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-a：冷重建跳过「本轮被禁用 / 隐藏闭合」的调用（与实时链路一致，无 part）。

    被测行为：一条 AIMessage（1 个正常调用 + 1 个隐藏闭合调用）+ 两条 ToolMessage，其中隐藏
    调用的结果行带 ``transport_metadata.hidden=True``（由 ``settle`` 写入）。
    修复后实际观测：只返回 ``normal-1`` 一个 part；隐藏调用被整条跳过。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    rows = [
        _ai_row(
            [
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ]
        ),
        _tool_row(
            2,
            "normal-1",
            "file body",
            {"status": "completed", "display_data": None, "error": None},
            sequence=2,
        ),
        _tool_row(
            3,
            "hidden-1",
            "This tool is disabled for the current run. Do not call again",
            {
                "status": "failed",
                "display_data": {"status_hint": "禁用"},
                "error": "本轮禁用",
                "hidden": True,
            },
            sequence=3,
        ),
    ]

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)

    assert set(parts) == {"normal-1"}, f"实际 part={sorted(parts)}"
    assert parts["normal-1"]["status"] == "completed"


def test_t4_cold_rebuild_hidden_call_without_result_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-b：**已知边界**——隐藏调用没有配套 ToolMessage 时，冷重建仍会建 part（cancelled）。

    ``hidden`` 标记只由 ``settle`` 随 ToolMessage 行写入；进程在 ``settle`` 之前终止（例如工具
    执行期崩溃、或被取消）时该行不存在，冷重建无从得知调用是隐藏的，只能按默认规则建一个
    ``cancelled`` part。此时 part 上**没有** ``isError`` / ``error`` 键（只在 ToolMessage 分支写入）。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    rows = [
        _ai_row(
            [
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ]
        ),
        _tool_row(
            2,
            "normal-1",
            "file body",
            {"status": "completed", "display_data": None, "error": None},
            sequence=2,
        ),
    ]

    parts = ConversationTaskStateRebuilder.build_pair_tool_part(rows)

    assert len(parts) == 2
    assert parts["hidden-1"]["status"] == "cancelled"
    assert "isError" not in parts["hidden-1"], f"part 键={sorted(parts['hidden-1'])}"
    assert "error" not in parts["hidden-1"]


def test_t4_live_and_cold_projection_agree_on_one_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-c：同一批调用，实时投影与冷重建**都是 1 个 part**（冷/热一致）。

    - 实时：``create`` 只为 ``allowed`` 桶（``read_file``）发 ``ToolCallCreatedEvent``，
      ``write_file``（已注册但不在 allows_tools）进 ``blocked_calls``、无创建事件、无 part；
    - 冷：``build_pair_tool_part`` 按结果行的 ``hidden`` 标记跳过隐藏调用。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        live = harness.manager.create(
            task_id=11,
            run_id=200,
            step_id="step-3",
            raw_tool_calls=[
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ],
        )

    live_part_ids = [event.tool_call_id for event in harness.writer.created_events]
    assert live_part_ids == ["normal-1"], f"实时创建的 part={live_part_ids}"
    assert "hidden-1" in live.blocked_calls

    rows = [
        _ai_row(
            [
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ]
        ),
        _tool_row(
            2,
            "normal-1",
            "file body",
            {"status": "completed", "display_data": None, "error": None},
            sequence=2,
        ),
        _tool_row(
            3,
            "hidden-1",
            "This tool is disabled for the current run. Do not call again",
            {
                "status": "failed",
                "display_data": {"status_hint": "禁用"},
                "error": "本轮禁用",
                "hidden": True,
            },
            sequence=3,
        ),
    ]
    cold_part_ids = sorted(ConversationTaskStateRebuilder.build_pair_tool_part(rows))

    assert live_part_ids == cold_part_ids == ["normal-1"], f"冷重建 part={cold_part_ids}"


def test_t4_rebuild_snapshot_materializes_only_the_visible_tool_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-d：整张快照 ``rebuild`` 后只出现可见调用（``normal-1``）的 part。

    实际观测：``runs[0].messages[-1].parts`` 里的 ``tool-call`` part 只有 ``normal-1``，
    隐藏的 ``hidden-1`` 不出现，整张快照通过 ``validate_snapshot``。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    run = SimpleNamespace(
        id=1,
        task_id=7,
        status="completed",
        end_reason=None,
        usage=None,
        error=None,
        input_text="",
        image_paths=None,
        extra=None,
        created_at=None,
    )
    task = SimpleNamespace(id=7, current_run_id=1, context_window_total=None)
    rows = [
        _ai_row(
            [
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ]
        ),
        _tool_row(
            2,
            "normal-1",
            "file body",
            {"status": "completed", "display_data": None, "error": None},
            sequence=2,
        ),
        _tool_row(
            3,
            "hidden-1",
            "This tool is disabled for the current run. Do not call again",
            {
                "status": "failed",
                "display_data": {"status_hint": "禁用"},
                "error": "本轮禁用",
                "hidden": True,
            },
            sequence=3,
        ),
    ]

    snapshot = ConversationTaskStateRebuilder.rebuild(task, [run], rows)
    parts = snapshot["runs"][0]["messages"][-1]["parts"]

    tool_part_ids = [part["toolCallId"] for part in parts if part["type"] == "tool-call"]
    assert tool_part_ids == ["normal-1"], f"快照 part={tool_part_ids}"
    validate_snapshot(snapshot)


def test_t4_orphan_tool_message_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-e：ToolMessage 找不到同 Run 内的 AI 工具调用时抛 RuntimeError（上下文被破坏）。

    实际观测：抛 ``RuntimeError("未闭合tool")``。
    是否符合预期：符合（冷重建对损坏上下文显式失败，不静默丢弃结果行）。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file",)),
    )
    rows = [
        _ai_row([{"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}}]),
        _tool_row(
            2,
            "orphan-9",
            "no matching ai tool call",
            {"status": "completed", "display_data": None, "error": None},
            sequence=2,
        ),
    ]

    with pytest.raises(RuntimeError, match="未闭合tool"):
        ConversationTaskStateRebuilder.build_pair_tool_part(rows)


def test_t4_build_run_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """T4-f：Run 错误契约的投影——合法 ``{code, message}`` 通过，畸形/非映射显式失败。

    实际观测：
    - ``{"code": "tool_error_limit", "message": "..."}`` → 原样投影；
    - 键集合不符 / 空白文案 / 非映射 → 抛 ``ValueError``。
    是否符合预期：符合（受控字段被污染时显式失败，而不是渲染出空错误气泡）。
    """

    ok = ConversationTaskStateRebuilder.build_run_error(
        SimpleNamespace(id=5, error={"code": "tool_error_limit", "message": "连续工具错误"})
    )
    assert ok == {"code": "tool_error_limit", "message": "连续工具错误"}
    assert (
        ConversationTaskStateRebuilder.build_run_error(SimpleNamespace(id=5, error=None)) is None
    )

    for bad in (
        SimpleNamespace(id=5, error=["not", "a", "mapping"]),
        SimpleNamespace(id=5, error={"code": "   ", "message": "x"}),
        SimpleNamespace(id=5, error={"code": "c", "message": "m", "extra": 1}),
    ):
        with pytest.raises(ValueError):
            ConversationTaskStateRebuilder.build_run_error(bad)


def test_t4_get_tool_display_uses_declaration_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-g：``get_tool_display`` 命中带 ``display`` 的注册工具时返回其序列化声明。

    实际观测：返回 ``{"verb": "读取文件", "icon": "eye"}``；空名 / 未注册 / 无 display 均返回 ``{}``。
    是否符合预期：符合（保证 part.presentation 恒为合法对象）。
    """

    class _Registry:
        @staticmethod
        def get_tool_definition(name: str) -> Any:
            if name != "read_file":
                return None
            return SimpleNamespace(display=SimpleNamespace(to_dict=lambda: {"verb": "读取文件"}))

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        _Registry,
    )
    assert ConversationTaskStateRebuilder.get_tool_display("read_file") == {"verb": "读取文件"}
    assert ConversationTaskStateRebuilder.get_tool_display("") == {}
    assert ConversationTaskStateRebuilder.get_tool_display("ghost_tool") == {}


def test_t4_delegation_display_data_backfills_child_locators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-h：委派结果的展示数据回填 child_task_id / child_run_id / agent_role。

    实际观测：``display_data.kind == "delegation-result"`` 时，part 上出现 ``child_task_id``
    与 ``child_run_id``；``display_data`` 无持久化 role 时按 ``child_agent_roles`` 回填，
    并把 role 写回 ``display_data["role"]`` 与 ``part["agent_role"]``。
    是否符合预期：符合（旧委派记录的 role 回填既定行为）。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("delegate_task",)),
    )
    rows = [
        _ai_row(
            [
                {
                    "id": "delegate-1",
                    "name": "delegate_task",
                    "args": {"child_agent_id": "reviewer", "message": "review"},
                }
            ]
        ),
        _tool_row(
            2,
            "delegate-1",
            "started",
            {
                "status": "completed",
                "display_data": {
                    "kind": "delegation-result",
                    "child_task_id": 22,
                    "child_run_id": 220,
                    "child_agent_id": "reviewer",
                },
                "error": None,
            },
            sequence=2,
        ),
    ]

    part = ConversationTaskStateRebuilder.build_pair_tool_part(
        rows, child_agent_roles={(22, "reviewer"): "workspace-reviewer"}
    )["delegate-1"]

    assert part["child_task_id"] == 22
    assert part["child_run_id"] == 220
    assert part["agent_role"] == "workspace-reviewer"
    assert part["display_data"]["role"] == "workspace-reviewer"


def test_t4_hidden_marksurvives_from_settle_to_cold_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-g（端到端）：``settle`` 写下的 ``hidden`` 标记能让冷重建跳过隐藏调用。

    把 D3 与 D4 接起来验证：``classify`` 把本轮未放行的 ``write_file`` 判进 ``blocked_calls``
    → ``settle`` 静默闭合、不发终态事件、结果行 metadata 带 ``hidden=True`` → 用该 metadata
    构造的 context 行经 ``build_pair_tool_part`` 重建时**不产出**该调用的 part。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    harness = _LifecycleHarness(
        registered_tool_names=("read_file", "write_file"),
        allows_tools=("read_file",),
    )
    with harness.runtime():
        classified = harness.manager.classify(
            tool_calls=[
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ],
            invalid_tool_calls=[],
        )
    assert list(classified.valid_calls) == ["normal-1"]
    assert list(classified.blocked_calls) == ["hidden-1"]

    with harness.runtime():
        classified.settle(
            task_id=11,
            run_id=200,
            step_id="step-3",
            summary=_summary(
                tool_call_id="hidden-1",
                tool_name="write_file",
                status="error",
                error="tool is not allowed: write_file",
            ),
        )

    # 事实 1：隐藏调用不发终态事件（前端无 part）。
    assert harness.writer.status_events == [], f"实发事件={harness.writer.status_events}"
    # 事实 2：结果行带 hidden 标记。
    assert harness.metadata[-1].get("hidden") is True
    # 事实 3：冷重建据此跳过该调用。
    rows = [
        _ai_row(
            [
                {"id": "normal-1", "name": "read_file", "args": {"path": "a.txt"}},
                {"id": "hidden-1", "name": "write_file", "args": {"path": "b.txt"}},
            ]
        ),
        _tool_row(
            2,
            "hidden-1",
            "tool is not allowed: write_file",
            harness.metadata[-1],
            sequence=2,
        ),
    ]
    assert set(ConversationTaskStateRebuilder.build_pair_tool_part(rows)) == {"normal-1"}
