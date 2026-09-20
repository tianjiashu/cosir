"""对抗测试：① 后端 logger 跨用例隔离修复；② fail_invalid_tools 新契约边界。

本文件只新增测试，不修改任何生产代码（``apps/backend/app/**``）。

分两组：
- A 组：证明 conftest 的 autouse 夹具确实复原 ``coding_agent.backend`` / ``uvicorn.error``
  的 ``level`` / ``propagate``，并攻击其可能的绕过面。
- B 组：攻击 ``ToolCallLifecycleManager.fail_invalid_tools`` 的 copy-on-write / 幂等 /
  事件失败 / 全终态 / 空集合等边界，以及 model 节点写回链路。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.workflows.react.nodes.helper.tool_call_lifecycle as lifecycle_module
from app.config.logging.configuration import configure_logging, shutdown_logging
from app.config.logging.logger import log as backend_log
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.core.workflows.react.nodes.helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)

# ---------------------------------------------------------------------------
# 通用桩
# ---------------------------------------------------------------------------


class _Harness:
    """为 lifecycle 提供最小运行时依赖，收集事件与模型消息。"""

    def __init__(self, model_tools: list[Any] | None = None) -> None:
        self.events: list[Any] = []
        self.messages: list[Any] = []
        self.operations = SimpleNamespace(
            to_tool_model_message=lambda observation: ("tool-message", observation.tool_call_id),
            model_tools=model_tools or [SimpleNamespace(name="read_file", display=None)],
        )
        self.runtime_config = SimpleNamespace(operations=self.operations)
        self.runtime_context = SimpleNamespace(add_message=lambda *a, **k: None)

    def ctx(self):
        from unittest.mock import patch

        return patch.multiple(
            lifecycle_module,
            _runtime_config=lambda: self.runtime_config,
            _runtime_context=lambda: self.runtime_context,
            get_stream_writer=lambda: self.events.append,
        )


def _invalid_record(call_id: str, *, status: str = "pending") -> ToolCallLifecycleRecord:
    return ToolCallLifecycleRecord(
        tool_call_id=call_id,
        tool_name="read_file",
        status=status,  # type: ignore[arg-type]
        invalid_detail={"name": "read_file", "args": "{", "error": "invalid json"},
    )


# ---------------------------------------------------------------------------
# A 组：日志隔离
# ---------------------------------------------------------------------------

BACKEND = "coding_agent.backend"


def test_polluting_case_leaves_global_state_dirty():
    """A1（前置）：本用例故意污染全局 logger，为 A2 提供污染源；不应被夹具阻止。"""

    logger = logging.getLogger(BACKEND)
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    # 直接断言：夹具不会在同用例内抢先复原（副作用被保留）。
    assert logger.propagate is False
    assert logger.level == logging.CRITICAL


def test_next_case_caplog_recovers_debug_events(caplog: pytest.LogCaptureFixture):
    """A2（回归）：上一用例把 propagate=False + level=CRITICAL 后，本用例 caplog 仍能抓 DEBUG。"""

    logger = logging.getLogger(BACKEND)
    # 夹具应在用例开始前复原到「未污染」的初始值（level=0/NOTSET, propagate=True）。
    assert logger.propagate is True, f"propagate 未复原: {logger.propagate}"
    assert logger.level == logging.NOTSET, f"level 未复原: {logger.level}"

    with caplog.at_level(logging.DEBUG, logger=BACKEND):
        logger.debug("isolation-probe-debug")
    assert any(
        "isolation-probe-debug" in r.message for r in caplog.records
    ), "caplog 未能抓到 coding_agent.backend 的 DEBUG 事件，隔离修复回归"


def test_configure_logging_then_next_case_can_capture(caplog: pytest.LogCaptureFixture, tmp_path):
    """A3（真实链路回归）：configure_logging + shutdown_logging 后，下一个用例仍可 caplog。"""

    configure_logging(tmp_path)
    backend_log.info("backend-file-log-event", extra={"msg": "写文件日志"})
    shutdown_logging()
    # 该用例内此时 propagate 已被 configure_logging 置 False（夹具仅跨用例复原）。
    assert logging.getLogger(BACKEND).propagate is False


def test_after_shutdown_logging_caplog_still_works(caplog: pytest.LogCaptureFixture):
    """A4（回归）：紧接 A3 的用例验证 shutdown 不复原 propagate 后夹具仍能救回 caplog。"""

    logger = logging.getLogger(BACKEND)
    assert logger.propagate is True, "configure_logging 留下的 propagate=False 未被夹具复原"

    with caplog.at_level(logging.DEBUG, logger=BACKEND):
        logger.debug("post-shutdown-probe")
    assert any("post-shutdown-probe" in r.message for r in caplog.records)


def test_fixture_does_not_over_restore_within_same_case():
    """A5（副作用）：同一用例内的自定义 propagate/level 设置必须被完整保留。"""

    logger = logging.getLogger(BACKEND)
    original = logger.propagate
    logger.propagate = not original
    logger.setLevel(logging.ERROR)
    try:
        assert logger.propagate is (not original)
        assert logger.level == logging.ERROR
    finally:
        logger.propagate = original
        logger.setLevel(logging.NOTSET)


def test_bypass_logger_disabled_flag(caplog: pytest.LogCaptureFixture):
    """A6（绕过面）：``logger.disabled`` 污染夹具不复原 —— 记录实际行为。

    若夹具只复原 level/propagate，则把 logger.disabled 置 True 的用例会让后续 caplog 失空。
    """

    logger = logging.getLogger(BACKEND)
    # 该用例自身先恢复 disabled，避免真的污染后续；此处只断言夹具是否覆盖该字段。
    logger.disabled = True
    try:
        with caplog.at_level(logging.DEBUG, logger=BACKEND):
            logger.debug("disabled-probe")
        captured = any("disabled-probe" in r.message for r in caplog.records)
        assert captured is False, "logger.disabled=True 时仍抓到了记录，前置假设不成立"
    finally:
        logger.disabled = False


def test_bypass_logging_disable(caplog: pytest.LogCaptureFixture):
    """A7（绕过面）：``logging.disable()`` 全局禁用夹具不复原 —— 记录实际行为。"""

    with caplog.at_level(logging.DEBUG, logger=BACKEND):
        logging.disable(logging.CRITICAL)
        try:
            logging.getLogger(BACKEND).debug("global-disable-probe")
        finally:
            logging.disable(logging.NOTSET)
    assert not any("global-disable-probe" in r.message for r in caplog.records)


def test_a9_snapshot_scope_covers_disabled_and_global_disable() -> None:
    """A9：夹具快照必须覆盖 level / propagate / disabled 与 ``logging.disable()`` 阈值。

    ``logger.propagate=False``、``logger.disabled=True`` 与 ``logging.disable()`` 三者都能让
    后续用例的 caplog 静默失空，缺任何一个都会留下隔离绕过面。
    """

    # 通过夹具实现文件路径导入，避免包名解析差异。
    import importlib.util
    from pathlib import Path

    conftest_path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("_adv_conftest", conftest_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    states, root_disable = mod._snapshot()
    assert isinstance(root_disable, int), "夹具必须快照 logging.disable() 阈值"
    assert states, "夹具必须至少覆盖后端 logger"
    for name, state in states.items():
        assert len(state) == 3, f"{name} 的快照项数={len(state)}（需覆盖 level/propagate/disabled）"
        assert isinstance(state[0], int)
        assert isinstance(state[1], bool)
        assert isinstance(state[2], bool)

    # 行为验证：先把两项置为**非默认**值再取快照，随后改回默认，最后确认 _restore 还原的是
    # 快照中的非默认值 —— 否则「硬编码回默认」的伪实现也能骗过断言。
    logger = logging.getLogger(BACKEND)
    logger.disabled = True
    logging.disable(logging.ERROR)
    snapshot = mod._snapshot()
    logger.disabled = False
    logging.disable(logging.NOTSET)
    try:
        mod._restore(snapshot)
        restored_disabled = logger.disabled
        restored_global = logging.root.manager.disable
    finally:
        logger.disabled = False
        logging.disable(logging.NOTSET)
    assert (
        restored_disabled is True
    ), "夹具未把 logger.disabled 复原为快照中的非默认值（硬编码回默认不具备隔离能力）"
    assert (
        restored_global == logging.ERROR
    ), "夹具未把 logging.disable() 阈值复原为快照中的非默认值（硬编码回 NOTSET 不具备隔离能力）"


# ---------------------------------------------------------------------------
# B 组：fail_invalid_tools 契约
# ---------------------------------------------------------------------------


def test_mixed_pending_and_failed_only_migrates_pending():
    """B1：混合 pending+已 failed 时，仅 pending 迁移、发一次终态，repair 只含被收口者。"""

    harness = _Harness()
    mgr = ToolCallLifecycleManager(
        calls={
            "p1": _invalid_record("p1", status="pending"),
            "p2": _invalid_record("p2", status="pending"),
            "f1": _invalid_record("f1", status="failed"),
        }
    )
    with harness.ctx():
        updated, repair = mgr.fail_invalid_tools(1, 2, "step-1")

    failed_events = [e for e in harness.events if getattr(e, "status", None) == "failed"]
    assert sorted(e.tool_call_id for e in failed_events) == ["p1", "p2"]
    assert {cid: r.status for cid, r in updated.calls.items()} == {
        "p1": "failed",
        "p2": "failed",
        "f1": "failed",
    }
    assert repair is not None
    # repair 只应描述被收口的 p1/p2 两条，不含已终态的 f1。
    assert repair.count("## read_file") == 2


def test_all_terminal_returns_equivalent_snapshot_and_none():
    """B2：全部已终态 → 等价新快照、repair=None、无事件。"""

    harness = _Harness()
    mgr = ToolCallLifecycleManager(
        calls={
            "f1": _invalid_record("f1", status="failed"),
            "c1": _invalid_record("c1", status="cancelled"),
        }
    )
    with harness.ctx():
        updated, repair = mgr.fail_invalid_tools(1, 2, "step-1")

    assert repair is None
    assert harness.events == []
    # copy-on-write 契约恒定：即使本批无待收口调用，返回的也是新快照（对象身份可预期）。
    assert updated is not mgr, "全终态时应返回新快照，而不是原对象引用"
    assert updated.calls == mgr.calls
    assert {cid: r.status for cid, r in updated.calls.items()} == {
        "f1": "failed",
        "c1": "cancelled",
    }


def test_no_calls_returns_equivalent_manager_and_none():
    """B3：calls 为空 → 返回类型/内容正确、repair=None、无事件。"""

    harness = _Harness()
    mgr = ToolCallLifecycleManager()
    with harness.ctx():
        updated, repair = mgr.fail_invalid_tools(1, 2, "step-1")

    assert isinstance(updated, ToolCallLifecycleManager)
    assert updated.calls == {}
    assert repair is None
    assert harness.events == []


def test_copy_on_write_origin_untouched_and_new_object():
    """B4：原 manager 不被就地改写（保持 pending），返回快照为新对象。"""

    harness = _Harness()
    mgr = ToolCallLifecycleManager(calls={"p1": _invalid_record("p1")})
    original_id = id(mgr)
    with harness.ctx():
        updated, _ = mgr.fail_invalid_tools(1, 2, "step-1")

    assert updated is not mgr
    assert id(mgr) == original_id  # 未替换
    assert mgr.calls["p1"].status == "pending", "原快照被就地改写，违反 copy-on-write"
    assert updated.calls["p1"].status == "failed"


def test_idempotent_second_call_no_event_and_none_repair():
    """B5：连续两次调用，第二次无事件、repair=None、状态一致。"""

    harness = _Harness()
    mgr = ToolCallLifecycleManager(calls={"p1": _invalid_record("p1")})
    with harness.ctx():
        first, repair1 = mgr.fail_invalid_tools(1, 2, "step-1")
        events_after_first = len(harness.events)
        # 幂等语义：对「已收口的返回值」再次收口。
        second, repair2 = first.fail_invalid_tools(1, 2, "step-1")

    assert repair1 is not None
    assert repair2 is None
    assert len(harness.events) == events_after_first, "第二次调用又发了事件"
    assert second.calls["p1"].status == "failed"


def test_emit_status_failure_does_not_escape_and_state_consistency():
    """B6（异常路径）：stream writer 抛异常时，方法行为与快照一致性。

    docstring 声明「异常：无」。若 writer 抛异常逃逸，则违反契约；若半迁移（状态已 failed
    但事件未发出），同样需要记录。
    """

    harness = _Harness()

    def _boom(_event: Any) -> None:
        raise RuntimeError("stream writer down")

    from unittest.mock import patch

    mgr = ToolCallLifecycleManager(calls={"p1": _invalid_record("p1")})
    escaped: Exception | None = None
    with patch.multiple(
        lifecycle_module,
        _runtime_config=lambda: harness.runtime_config,
        _runtime_context=lambda: harness.runtime_context,
        get_stream_writer=lambda: _boom,
    ):
        try:
            _updated, _repair = mgr.fail_invalid_tools(1, 2, "step-1")
        except Exception as exc:
            escaped = exc

    assert (
        escaped is None
    ), f"fail_invalid_tools 让异常逃逸，违反 docstring「异常：无」: {escaped!r}"


# ---------------------------------------------------------------------------
# C 组：全量/随机稳定性对照（由外部命令覆盖，这里只加一个顺序无关自检）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("first", [True, False])
def test_logger_state_restored_regardless_of_order(caplog: pytest.LogCaptureFixture, first: bool):
    """C1：无论顺序如何，用例开始前 logger 都是干净的（level=NOTSET, propagate=True）。"""

    logger = logging.getLogger(BACKEND)
    assert logger.level == logging.NOTSET
    assert logger.propagate is True
    with caplog.at_level(logging.DEBUG, logger=BACKEND):
        logger.debug(f"order-probe-{first}")
    assert any(f"order-probe-{first}" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# D 组：model 节点真实调用链写回
# ---------------------------------------------------------------------------


def test_model_node_writes_failed_invalid_call_into_lifecycle(monkeypatch: Any) -> None:
    """D1（真实链路）：model 节点返回的 lifecycle 中非法调用应为 failed（非 pending）。"""

    import asyncio
    from collections.abc import AsyncIterator

    from langchain_core.messages import AIMessage, AIMessageChunk

    from app.core.context.runtime_context_manager import _as_ai_message
    from app.core.workflows.react.nodes import model_node as model_module
    from app.core.workflows.react.state import ReactGraphState

    task_runtime_spaces.get_or_create(1).take_deferred_system_messages()

    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "read_file", "args": "{", "id": "call-1", "error": "invalid json"}
        ],
    )
    events: list[Any] = []
    messages: list[Any] = []

    async def _astream(_messages: list[Any]) -> AsyncIterator[AIMessageChunk]:
        yield AIMessageChunk(content="")

    operations = SimpleNamespace(
        model_tools=[SimpleNamespace(name="read_file", display=None)],
        get_current_run=lambda: SimpleNamespace(task_id=1, id=2),
        is_current_run_cancelled=lambda: False,
        complete_run_if_running=lambda *a, **k: object(),
        fail_run_if_running=lambda *a, **k: object(),
    )
    runtime_config = SimpleNamespace(
        operations=operations,
        model=SimpleNamespace(astream=_astream),
        thinking_channel="",
        thinking_roundtrip=True,
        run=SimpleNamespace(task_id=1, id=2),
        usage_stats=SimpleNamespace(add_usage_metadata=lambda m: None, to_dict=lambda: {}),
    )
    merged: dict[str, AIMessageChunk] = {}

    def add_message(m: Any, **_k: Any) -> None:
        messages.append(m)

    def add_message_chunk(
        chunk: AIMessageChunk, *, stream_id: str, run_id: Any = None
    ) -> AIMessage:
        merged["c"] = chunk if "c" not in merged else merged["c"] + chunk
        return _as_ai_message(merged["c"])

    def flush_message_chunk(*, stream_id: str, run_id: Any = None, mode: str = "running"):
        if mode == "complete":
            messages.append(message)
            return message
        return None

    runtime_context = SimpleNamespace(
        load_message=lambda: [],
        add_message=add_message,
        add_message_chunk=add_message_chunk,
        flush_message_chunk=flush_message_chunk,
    )
    monkeypatch.setattr(model_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(model_module, "_runtime_context", lambda: runtime_context)
    monkeypatch.setattr(model_module, "get_stream_writer", lambda: events.append)
    monkeypatch.setattr(lifecycle_module, "_runtime_config", lambda: runtime_config)
    monkeypatch.setattr(lifecycle_module, "_runtime_context", lambda: runtime_context)
    monkeypatch.setattr(lifecycle_module, "get_stream_writer", lambda: events.append)

    state = ReactGraphState(
        step_count=1,
        tool_error_count=0,
        requested_tool=False,
        continue_model=False,
        final_response=False,
        terminal=False,
        instruction="",
        max_steps=10,
        final_text="",
        last_tool_results={"instruction": "", "observations": []},
    )
    result = asyncio.run(model_module._model_node(state))
    task_runtime_spaces.get_or_create(1).take_deferred_system_messages()

    lifecycle = result["tool_call_lifecycle"]
    assert (
        lifecycle.calls["call-1"].status == "failed"
    ), f"model 节点写回的非法调用状态为 {lifecycle.calls['call-1'].status}（应为 failed）"
    assert any(
        getattr(e, "status", None) == "failed" and getattr(e, "tool_call_id", None) == "call-1"
        for e in events
    ), "model 节点未为非法调用发出 failed 终态事件"
