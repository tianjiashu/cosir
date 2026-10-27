"""工具准入与隐藏调用的冷重建测试；使用内存假对象隔离模型和数据库。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.tool.execute.tool_access_gate as gate_module
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.events import ToolCallCreatedEvent, ToolCallStatusChangedEvent
from app.core.hook.hook_event import HookDecision
from app.core.hook.hook_result import HookResult
from app.core.tool.schemas import (
    ToolCall,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.execute.tool_access_gate import ToolAccessGate
from app.tools.tool_handler.read_file import ReadFileTool
from app.core.tool.tool_registry import ToolRegistry
from app.models.conversation_run_options import ConversationRunOptions
from app.models.conversation_task_context import ConversationTaskContextRecord

# ============================================================================
# 共享装配
# ============================================================================


class _FakeStreamWriter:
    """收集工具链路事件的假 stream writer。"""

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


def _observation(**overrides: Any) -> ToolObservation:
    """构造最小 ``ToolObservation``（供假工具批次执行返回）。"""

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


def _execution_context(tmp_path: Any) -> ToolExecutionContext:
    """构造门禁所需的最小执行边界（不触碰真实文件系统）。"""

    return ToolExecutionContext(
        step_id="step-1",
        task_id=11,
        workspace_id=1,
        workspace_root=tmp_path,
        run_id=200,
        tool_call_id="call-1",
    )


# ============================================================================
# T1. ``_tools_node`` 重入重放
# ============================================================================


# ============================================================================
# T2. ``ToolAccessGate.evaluate`` 检查顺序
# ============================================================================


def test_t2_disabled_and_invalid_args_yields_argument_denial(tmp_path: Any) -> None:
    """T2-a：「已注册但不在 allowed_tool_names」∩「参数非法」→ 必须命中**权限门禁** denial。

    被测行为：``evaluate`` 的四段检查实际执行顺序（注册表 → 权限门禁 → 参数校验 → Hook）。
    修复前实际观测（缺陷）：denial.content 以 ``"invalid tool arguments: "`` 开头 ——
    参数校验排在权限门禁之前，被禁用的工具会向模型回传
    「改改参数就能重试」的错误信号。
    修复后（当前断言）：权限门禁先于参数校验，参数是否合法都不影响命中「本轮禁用」分支。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(
            tool_name="read_file", arguments='{"nonexistent_field": 1}', call_id="call-disabled"
        ),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"other_tool"},  # read_file 已注册但本轮被禁用
    )

    assert outcome.admitted is False
    assert outcome.tool is None
    assert outcome.denial is not None
    # 关键事实：命中的是「本轮禁用」分支，而不是「参数非法」分支。
    assert outcome.denial.content.startswith("tool is not allowed: read_file"), outcome.denial.content
    assert "disabled for the current run" in outcome.denial.content
    assert outcome.denial.tool_call_id == "call-disabled"


def test_t2_disabled_with_valid_args_yields_disabled_denial(tmp_path: Any) -> None:
    """T2-b：对照用例——参数合法 + 本轮被禁用 → 同一个「本轮禁用」denial（retryable=False）。

    与 T2-a 断言完全同值，才说明「是否被禁用」不再取决于参数是否恰好合法（修复前 T2-a 与
    T2-b 会命中两个不同分支，同一条禁用策略对模型的呈现不稳定）。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="read_file", arguments='{"path": "a.txt"}', call_id="call-disabled-2"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"other_tool"},
    )

    assert outcome.admitted is False
    assert outcome.denial is not None
    assert outcome.denial.content.startswith("tool is not allowed: read_file")
    assert "disabled for the current run" in outcome.denial.content


def test_t2_unknown_tool_wins_over_everything(tmp_path: Any) -> None:
    """T2-c：未注册工具名 = 最优先拒绝（unknown tool），不看 allowed 集合。

    实际观测：content 以 ``"unknown tool: "`` 开头，并提示「该工具未注册」。
    是否符合预期：符合（注册表命中是最前置检查，与 docstring 一致）。
    """

    registry = ToolRegistry([ReadFileTool().to_definition()])
    gate = ToolAccessGate(registry)
    outcome = gate.evaluate(
        ToolCall(tool_name="ghost_tool", arguments='{}', call_id="call-ghost"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names=set(),  # 连空白名单都要先让位给「注册表未命中」
    )

    assert outcome.admitted is False
    assert outcome.denial is not None
    assert outcome.denial.content.startswith("unknown tool: ")


def test_t2_pre_tool_use_hook_deny_and_argument_rewrite(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2-d：PreToolUse Hook 是最后一段检查——DENY 硬拒绝、modified_arguments 改写入参。

    实际观测：
    - DENY：``admitted=False``，denial.content 以 ``error: <deny_reason>`` 开头，且 ``tool`` 字段为 None；
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
        ToolCall(tool_name="read_file", arguments='{"path": "a.txt"}', call_id="call-hook"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"read_file"},
    )
    assert denied.admitted is False
    assert denied.tool is None
    assert denied.denial is not None
    assert denied.denial.content.startswith("blocked by policy")
    assert denied.denial.tool_call_id == "call-hook"

    class _RewriteHook:
        @staticmethod
        def safe_fire(_context: Any) -> HookResult:
            return HookResult(decision=HookDecision.ALLOW, modified_arguments={"path": "z.txt"})

    monkeypatch.setattr(gate_module, "HookInterceptor", _RewriteHook)
    rewritten = gate.evaluate(
        ToolCall(tool_name="read_file", arguments='{"path": "a.txt"}', call_id="call-hook-2"),
        execution_context=_execution_context(tmp_path),
        allowed_tool_names={"read_file"},
    )
    assert rewritten.admitted is True
    assert rewritten.arguments == {"path": "z.txt"}


# ============================================================================
# T3. ``settle`` 对「工具名未注册」记录是否发终态事件
# ============================================================================


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
        message={"role": "assistant", "content": "", "tool_calls": tool_calls},
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
        message={"role": "tool", "content": content, "tool_call_id": tool_call_id},
        include_in_context=False,
        sequence=sequence,
        metadata=metadata,
    )


def test_t4_cold_rebuild_skips_the_hidden_disabled_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4-a：冷重建跳过「本轮被禁用 / 隐藏闭合」的调用（与实时链路一致，无 part）。

    被测行为：一条 AIMessage（1 个正常调用 + 1 个隐藏闭合调用）+ 两条 ToolMessage，其中隐藏
    调用的结果行带 ``metadata.hidden=True``（由 ``settle`` 写入）。
    修复后实际观测：只返回 ``normal-1`` 一个 part；隐藏调用被整条跳过。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    rows = [
        _ai_row(
            [
                {
                    "id": "normal-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
                },
                {
                    "id": "hidden-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "b.txt"})},
                },
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
    ``cancelled`` part。此时 part 上**没有** ``isError`` / ``error`` 键
    （只在 ToolMessage 分支写入）。
    """

    monkeypatch.setattr(
        "app.assistant_transport.service.conversation_task_state_rebuilder.get_tool_registry",
        lambda: _FakeRebuildRegistry(("read_file", "write_file")),
    )
    rows = [
        _ai_row(
            [
                {
                    "id": "normal-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
                },
                {
                    "id": "hidden-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "b.txt"})},
                },
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
        context_window_total=None,
        model_config_id=None,
        options=ConversationRunOptions(),
        image_paths=None,
        extra=None,
        created_at=None,
    )
    task = SimpleNamespace(id=7, current_run_id=1, context_window_total=None)
    rows = [
        _ai_row(
            [
                {
                    "id": "normal-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
                },
                {
                    "id": "hidden-1",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "b.txt"})},
                },
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
    parts = snapshot["messages"][-1]["parts"]

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
        _ai_row(
            [
                {
                    "id": "normal-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})},
                }
            ]
        ),
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
    assert ConversationTaskStateRebuilder.build_run_error(SimpleNamespace(id=5, error=None)) is None

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

    实际观测：返回 ``{"verb": "读取文件", "icon": "eye"}``；
    空名 / 未注册 / 无 display 均返回 ``{}``。
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
    """T4-h：委派结果的展示数据回填 child_task_id / agent_role（子 Agent 按 Task 寻址，无 run_id）。

    实际观测：``display_data.kind == "delegation-result"`` 时，part 上出现 ``child_task_id``；
    ``display_data`` 使用持久化 role，并把 role 写回 ``display_data["role"]`` 与 ``part["agent_role"]``。
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
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps(
                            {
                                "child_agent_id": "reviewer",
                                "role": "workspace-reviewer",
                                "message": "review",
                            }
                        ),
                    },
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
                    "child_agent_id": "reviewer",
                    "role": "workspace-reviewer",
                },
                "error": None,
            },
            sequence=2,
        ),
    ]

    part = ConversationTaskStateRebuilder.build_pair_tool_part(rows)["delegate-1"]

    assert part["child_task_id"] == 22
    assert part["agent_role"] == "workspace-reviewer"
    assert part["display_data"]["role"] == "workspace-reviewer"
