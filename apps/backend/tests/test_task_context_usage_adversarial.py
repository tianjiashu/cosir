"""任务级上下文真实占用显示：对抗测试与边界测试（扩展版）。

本文件在既有 test_task_context_usage_adversarial.py 之上，额外覆盖：
- ContextUsage 值对象层契约（负值 / 非正 total / 边界 0）。
- TaskService.update_context_usage 的 public API 防御（clamp、正常、超大、last-write-wins）。
- _emit_context_usage 在合法计量、回写异常时的 fail-safe 行为（含精确日志断言）。
- get_task 在 agent 命中 / 未命中 / resolve 返回 0 时 context_window_total 的 0 与 None 区分。
- 错误日志确实产生（检查 caplog 记录而非仅断言不崩溃）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas.response.TaskResponse import TaskResponse
from app.api.tasks_api import get_task
from app.config.configuration import build_agent_registry, set_agent_registry
from app.config.settings import Settings
from app.core.llm.context_window_resolver import resolve_context_window
from app.core.workflows.nodes import model_node
from app.models import TaskRecord
from app.models.context_usage import ContextUsage
from app.service.depends import (
    close_service_dependencies,
    initialize_service_dependencies,
    reset_service_dependencies,
)
from app.service.task.task_service import TaskService
from app.service.task.workspace_service import WorkspaceService
from app.storage.store_engines import init_storage

NODE = "app.core.workflows.nodes.model_node"


@pytest.fixture
def storage_stack(tmp_path: Path):
    db_file = tmp_path / "app.sqlite3"
    log_db_file = tmp_path / "logs.sqlite3"
    checkpoint_file = tmp_path / "langgraph_checkpoints.sqlite"
    log_dir = tmp_path / "logs"
    Settings.override(
        DATABASE_FILE=db_file,
        LOG_DATABASE_FILE=log_db_file,
        CHECKPOINT_FILE=checkpoint_file,
        LOG_DIR=log_dir,
    )
    init_storage()
    initialize_service_dependencies()
    set_agent_registry(build_agent_registry())
    yield
    close_service_dependencies()
    reset_service_dependencies()
    Settings.load()


def _create_workspace() -> str:
    workspace = WorkspaceService().create_workspace(
        name="ws_ctx_adv", root_path="H:/ws_ctx_adv",
    )
    return workspace.workspace_id


def _create_task() -> TaskRecord:
    task_service = TaskService()
    task, _ = task_service.create_task_with_initial_turn(
        workspace_id=_create_workspace(), agent_id="developer",
        input_text="ctx adversarial task",
    )
    return task


# ---------------------------------------------------------------------------
# ContextUsage 值对象层契约
# ---------------------------------------------------------------------------

def test_context_usage_negative_used_tokens_minus_one_raises() -> None:
    # 测试目的：验证值对象层对极小负值(-1)的契约守卫；可能发现的缺陷：__post_init__ 漏校验边界值。
    with pytest.raises(ValueError):
        ContextUsage(used_tokens=-1, total_tokens=1000)


def test_context_usage_negative_used_tokens_minus_one_thousand_raises() -> None:
    # 测试目的：验证值对象层对极端负值(-1000)的契约守卫；可能发现的缺陷：仅校验了部分负值分支。
    with pytest.raises(ValueError):
        ContextUsage(used_tokens=-1000, total_tokens=1000)


def test_context_usage_zero_total_raises() -> None:
    # 测试目的：验证 total_tokens=0 在值对象层被拒绝；可能发现的缺陷：条件误写为 < 0 而非 <= 0。
    with pytest.raises(ValueError):
        ContextUsage(used_tokens=0, total_tokens=0)


def test_context_usage_negative_total_raises() -> None:
    # 测试目的：验证 total_tokens=-5 在值对象层被拒绝；可能发现的缺陷：total 分支未做负值校验。
    with pytest.raises(ValueError):
        ContextUsage(used_tokens=10, total_tokens=-5)


def test_context_usage_zero_used_one_total_legal() -> None:
    # 测试目的：验证边界值 used=0/total=1 合法（空上下文 + 最小正窗口）；可能发现的缺陷：误把 0 也判非法。
    usage = ContextUsage(used_tokens=0, total_tokens=1)
    assert usage.used_tokens == 0
    assert usage.total_tokens == 1


def test_context_usage_positive_legal() -> None:
    # 测试目的：验证正常正数值可构造；可能发现的缺陷：构造时误触发校验。
    usage = ContextUsage(used_tokens=500, total_tokens=8000)
    assert usage.used_tokens == 500
    assert usage.total_tokens == 8000


def test_context_usage_frozen_value_object() -> None:
    # 测试目的：验证 frozen dataclass 不可变，防止运行时被误改后绕过校验；可能发现的缺陷：误用可变 dataclass。
    usage = ContextUsage(used_tokens=1, total_tokens=2)
    with pytest.raises(Exception):
        usage.used_tokens = -1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TaskService.update_context_usage：public API 防御与正常路径
# ---------------------------------------------------------------------------

def test_update_context_usage_negative_token_clamped_to_zero(storage_stack: Path, caplog) -> None:
    # 测试目的：验证 public API 对负 used 的 clamp 兜底且产生 warning 日志（双层校验之一）。
    # 可能发现的缺陷：clamp 失效导致脏数据落库，或仅 clamp 未记录日志（弱观测性）。
    task = _create_task()
    with caplog.at_level(logging.WARNING):
        updated = TaskService().update_context_usage(task.task_id, -50)
    assert updated.context_usage_used == 0
    assert TaskService().get_task(task.task_id).context_usage_used == 0
    assert any(
        "context_usage_negative_clamped" in r.message for r in caplog.records
    ), "应记录 context_usage_negative_clamped 警告日志"


def test_update_context_usage_zero_token(storage_stack: Path) -> None:
    # 测试目的：验证 used=0 正常落库（边界值）；可能发现的缺陷：0 被误判为负而 clamp。
    task = _create_task()
    updated = TaskService().update_context_usage(task.task_id, 0)
    assert updated.context_usage_used == 0
    assert TaskService().get_task(task.task_id).context_usage_used == 0


def test_update_context_usage_huge_token_no_overflow(storage_stack: Path) -> None:
    # 测试目的：验证超大值 10**9 正确落库不溢出；可能发现的缺陷：整型/列类型溢出导致静默截断。
    task = _create_task()
    big = 10 ** 9
    updated = TaskService().update_context_usage(task.task_id, big)
    assert updated.context_usage_used == big
    assert TaskService().get_task(task.task_id).context_usage_used == big


def test_update_context_usage_positive_normal(storage_stack: Path) -> None:
    # 测试目的：验证普通正数值正确落库并回填 TaskRecord；可能发现的缺陷：返回值字段映射错误。
    task = _create_task()
    before = task.updated_at
    updated = TaskService().update_context_usage(task.task_id, 1234)
    assert isinstance(updated, TaskRecord)
    assert updated.task_id == task.task_id
    assert updated.context_usage_used == 1234
    assert updated.updated_at >= before


def test_update_context_usage_repeated_same_value_consistent(storage_stack: Path) -> None:
    # 测试目的：验证重复相同值不漂移；可能发现的缺陷：重复写导致累加或状态异常。
    task = _create_task()
    t = task.task_id
    for _ in range(20):
        TaskService().update_context_usage(t, 999)
    assert TaskService().get_task(t).context_usage_used == 999


def test_update_context_usage_interleaved_tasks_isolation(storage_stack: Path) -> None:
    # 测试目的：验证多任务交错更新互不污染；可能发现的缺陷：上下文串号导致写入错误任务。
    t1 = _create_task()
    t2 = _create_task()
    TaskService().update_context_usage(t1.task_id, 111)
    TaskService().update_context_usage(t2.task_id, 222)
    TaskService().update_context_usage(t1.task_id, 333)
    assert TaskService().get_task(t1.task_id).context_usage_used == 333
    assert TaskService().get_task(t2.task_id).context_usage_used == 222


def test_update_context_usage_sequential_updates_keep_last(storage_stack: Path) -> None:
    # 测试目的：验证并发/重复调用为 last-write-wins（仅保留最后一次）；可能发现的缺陷：非幂等导致取到旧值。
    task = _create_task()
    t = task.task_id
    a = TaskService().update_context_usage(t, 100)
    b = TaskService().update_context_usage(t, 200)
    c = TaskService().update_context_usage(t, 300)
    assert c.context_usage_used == 300
    assert c.updated_at >= b.updated_at >= a.updated_at
    assert TaskService().get_task(t).context_usage_used == 300


def test_update_context_usage_roundtrip_via_get(storage_stack: Path) -> None:
    # 测试目的：验证落库后经 get_task 回读一致；可能发现的缺陷：读路径映射遗漏字段。
    task = _create_task()
    TaskService().update_context_usage(task.task_id, 42)
    reread = TaskService().get_task(task.task_id)
    assert reread.context_usage_used == 42


def test_update_context_usage_unknown_task_raises_keyerror(storage_stack: Path) -> None:
    # 测试目的：验证不存在的 task_id 抛 KeyError（契约：异常路径应暴露而非静默）；可能发现的缺陷：吞异常返回 None。
    with pytest.raises(KeyError):
        TaskService().update_context_usage("task_does_not_exist", 1)


# ---------------------------------------------------------------------------
# _emit_context_usage：fail-safe 行为 + 精确日志断言
# ---------------------------------------------------------------------------

def test_emit_context_usage_meter_none_is_safe_noop(storage_stack: Path) -> None:
    # 测试目的：meter 为 None 时静默跳过，不写事件、不回写；可能发现的缺陷：None 路径误触发事件。
    runtime_context = MagicMock()
    runtime_context.usage_meter = None
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get:
        model_node._emit_context_usage(step_id="s1", task_id="any")
        mock_write.assert_not_called()
        mock_get.assert_not_called()


def test_emit_context_usage_meter_read_returns_none_is_safe(storage_stack: Path, caplog) -> None:
    # 测试目的：meter 返回 None（预期边界）只记 warning 且跳过；可能发现的缺陷：None 被当作合法 usage 继续回写。
    meter = MagicMock()
    meter.read.return_value = None
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         caplog.at_level(logging.WARNING):
        model_node._emit_context_usage(step_id="s2", task_id="any")
        mock_write.assert_not_called()
        assert any("context_usage_meter_empty" in r.message for r in caplog.records)


def test_emit_context_usage_meter_read_raises_only_logs(storage_stack: Path, caplog) -> None:
    # 测试目的：meter 抛异常时只记日志不中断；可能发现的缺陷：异常上抛中断模型节点。
    meter = MagicMock()
    meter.read.side_effect = RuntimeError("meter boom")
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         caplog.at_level(logging.ERROR):
        model_node._emit_context_usage(step_id="s3", task_id="any")
        mock_write.assert_not_called()
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_emit_context_usage_legal_usage_event_payload_precise(storage_stack: Path) -> None:
    # 测试目的：meter 返回合法 ContextUsage 时事件 payload 字段精确匹配且 service 被正确调用。
    # 可能发现的缺陷：事件 payload 用错字段（如把 total 当 used）、回写调用参数错误。
    task = _create_task()
    usage = ContextUsage(used_tokens=777, total_tokens=8000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}.get_task_service") as mock_get, \
         patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write:
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="step_1", task_id=task.task_id)
        assert mock_write.call_count == 1
        event_type, payload = mock_write.call_args.args
        assert event_type.name == "CONTEXT_USAGE"
        assert payload.used_tokens == 777
        assert payload.total_tokens == 8000
        mock_service.update_context_usage.assert_called_once_with(task.task_id, 777)


def test_emit_context_usage_legal_usage_persists_real(storage_stack: Path) -> None:
    # 测试目的：meter 返回合法 ContextUsage 经真实 service 回写成功落库（不 mock service 路径）。
    # 可能发现的缺陷：真实回写分支缺失、task_id 错传导致落库失败。
    task = _create_task()
    usage = ContextUsage(used_tokens=777, total_tokens=8000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event"):
        model_node._emit_context_usage(step_id="step_1", task_id=task.task_id)
    persisted = TaskService().get_task(task.task_id)
    assert persisted.context_usage_used == 777


def test_emit_context_usage_persist_failure_only_logs(storage_stack: Path, caplog) -> None:
    # 测试目的：service 回写抛异常时只记 error 日志、不中断、write_event 仍发生。
    # 可能发现的缺陷：回写失败导致整个节点崩溃、或事件也未发出。
    usage = ContextUsage(used_tokens=7, total_tokens=1000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.ERROR):
        mock_service = MagicMock()
        mock_service.update_context_usage.side_effect = RuntimeError("db down")
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="s6", task_id="any")
        mock_write.assert_called_once()
        assert any("context_usage_task_persist_failed" in r.message for r in caplog.records)


def test_emit_context_usage_empty_task_id_does_not_crash(storage_stack: Path, caplog) -> None:
    # 测试目的：空 task_id 在回写抛异常时只记 error 日志、事件仍发出；可能发现的缺陷：空 id 路径崩溃。
    usage = ContextUsage(used_tokens=10, total_tokens=1000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.ERROR):
        mock_service = MagicMock()
        mock_service.update_context_usage.side_effect = KeyError("")
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="s4", task_id="")
        mock_write.assert_called_once()
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_emit_context_usage_none_task_id_does_not_crash(storage_stack: Path, caplog) -> None:
    # 测试目的：None task_id 在回写抛异常时只记 error 日志、事件仍发出；可能发现的缺陷：None id 路径崩溃。
    usage = ContextUsage(used_tokens=10, total_tokens=1000)
    meter = MagicMock()
    meter.read.return_value = usage
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.ERROR):
        mock_service = MagicMock()
        mock_service.update_context_usage.side_effect = KeyError(None)
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="s5", task_id=None)
        mock_write.assert_called_once()
        assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_emit_context_usage_negative_token_rejected_by_value_object(storage_stack: Path) -> None:
    # 测试目的：负占用在值对象层被拦截（构造即抛），不会进入事件/持久化；可能发现的缺陷：校验位置错误导致负值流入。
    with pytest.raises(ValueError):
        ContextUsage(used_tokens=-7, total_tokens=1000)


# ---------------------------------------------------------------------------
# get_task：context_window_total 的 0 与 None 区分
# ---------------------------------------------------------------------------

def test_get_task_returns_context_window_total(storage_stack: Path) -> None:
    # 测试目的：agent profile 命中时 context_window_total 为动态计算的正数；可能发现的缺陷：total 恒为 None。
    task = _create_task()
    model_name = build_agent_registry().resolve(task.agent_id).model_name
    expected_total = resolve_context_window(model_name)
    resp = asyncio.run(get_task(task.task_id, TaskService()))
    assert resp.context_usage_used is None
    assert resp.context_window_total == expected_total
    assert resp.context_window_total is not None


def test_get_task_unknown_agent_degrades_to_none_total(storage_stack: Path) -> None:
    # 测试目的：agent 未命中（resolve 返回 None）时 total 应降级为 None；可能发现的缺陷：None profile 仍尝试 resolve 报错。
    task = _create_task()
    bad_record = TaskRecord(
        task_id=task.task_id, workspace_id=task.workspace_id,
        agent_id="nonexistent_agent", title=task.title,
        status=task.status, execution_status=task.execution_status,
        task_type=task.task_type, parent_task_id=task.parent_task_id,
        parent_turn_id=task.parent_turn_id, delegation_id=task.delegation_id,
        context_usage_used=task.context_usage_used,
        created_at=task.created_at, updated_at=task.updated_at,
    )
    with patch(
        "app.service.task.task_service.TaskService.get_task", return_value=bad_record
    ), patch("app.config.configuration.get_agent_registry") as mock_registry:
        mock_registry.return_value.resolve.return_value = None
        resp = asyncio.run(get_task(task.task_id, TaskService()))
        assert resp.context_window_total is None


def test_get_task_resolves_to_minuscule_total(storage_stack: Path) -> None:
    # 测试目的：resolve 返回极小值(1)时 total 恒为 1 且不为 None；可能发现的缺陷：极小值被误判为 None。
    task = _create_task()
    with patch("app.config.configuration.get_agent_registry") as reg, \
         patch("app.api.tasks_api.resolve_context_window", return_value=1):
        profile = MagicMock()
        profile.model_name = "some-model"
        reg.return_value.resolve.return_value = profile
        resp = asyncio.run(get_task(task.task_id, TaskService()))
        assert resp.context_window_total == 1
        assert resp.context_window_total is not None


def test_get_task_resolve_returns_profile_but_total_zero(storage_stack: Path) -> None:
    # 测试目的：resolve 命中但 window 为 0 时 total == 0（与 None 严格区分）；可能发现的缺陷：0 被错误当 None。
    task = _create_task()
    with patch("app.config.configuration.get_agent_registry") as reg, \
         patch("app.api.tasks_api.resolve_context_window", return_value=0):
        profile = MagicMock()
        profile.model_name = "m"
        reg.return_value.resolve.return_value = profile
        resp = asyncio.run(get_task(task.task_id, TaskService()))
        assert resp.context_window_total == 0
        assert resp.context_window_total is not None


def test_get_task_resolve_raises_degrades_to_none(storage_stack: Path) -> None:
    # 测试目的：agent registry / resolve 抛异常时 total 降级为 None 且任务仍正常返回（不崩溃）。
    # 可能发现的缺陷：resolve 异常未捕获导致 500。
    task = _create_task()
    with patch("app.config.configuration.get_agent_registry") as reg, \
         patch("app.api.tasks_api.resolve_context_window") as mock_resolve:
        reg.return_value.resolve.return_value = MagicMock(model_name="m")
        mock_resolve.side_effect = RuntimeError("catalog broken")
        resp = asyncio.run(get_task(task.task_id, TaskService()))
        assert resp.context_window_total is None
        assert resp.task_id == task.task_id


def test_get_task_unknown_task_id_raises_404(storage_stack: Path) -> None:
    # 测试目的：不存在的 task_id 返回 404；可能发现的缺陷：错误状态码或泄露原始 KeyError。
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_task("no_such_task_id", TaskService()))
    assert exc_info.value.status_code == 404


def test_get_task_unknown_task_id_does_not_raise_raw_keyerror(storage_stack: Path) -> None:
    # 测试目的：缺失任务时不应向上抛原始 KeyError；可能发现的缺陷：异常类型未转换。
    with pytest.raises(Exception) as exc_info:
        asyncio.run(get_task("missing_404", TaskService()))
    assert not isinstance(exc_info.value, KeyError)


# ---------------------------------------------------------------------------
# TaskResponse 映射（0 与 None 区分的端点层保障）
# ---------------------------------------------------------------------------

def test_task_response_from_record_default_total_none(storage_stack: Path) -> None:
    # 测试目的：无 total 注入时 total 为 None；可能发现的缺陷：默认误给 0。
    task = _create_task()
    resp = TaskResponse.from_record(task)
    assert resp.context_window_total is None
    assert resp.context_usage_used is None


def test_task_response_from_record_zero_total_is_zero(storage_stack: Path) -> None:
    # 测试目的：注入 total=0 时响应里是 0（非 None）；可能发现的缺陷：0 被吞成 None。
    task = _create_task()
    TaskService().update_context_usage(task.task_id, 5)
    latest = TaskService().get_task(task.task_id)
    resp = TaskResponse.from_record(latest, context_window_total=0)
    assert resp.context_window_total == 0
    assert resp.context_window_total is not None
    assert resp.context_usage_used == 5


def test_task_response_from_record_with_used_and_total(storage_stack: Path) -> None:
    # 测试目的：used 与 total 同时正确映射，供前端占比计算；可能发现的缺陷：字段错位。
    task = _create_task()
    TaskService().update_context_usage(task.task_id, 321)
    latest = TaskService().get_task(task.task_id)
    resp = TaskResponse.from_record(latest, context_window_total=8000)
    assert resp.context_usage_used == 321
    assert resp.context_window_total == 8000
    assert 0 < resp.context_usage_used <= resp.context_window_total


# ---------------------------------------------------------------------------
# resolve_context_window 边界
# ---------------------------------------------------------------------------

def test_resolve_context_window_empty_model_name_returns_fallback() -> None:
    # 测试目的：空 model_name 走兜底（128_000）；可能发现的缺陷：空串查表抛异常或返回 0。
    assert resolve_context_window("") == 128_000


def test_resolve_context_window_unknown_model_fallback() -> None:
    # 测试目的：未收录模型走兜底；可能发现的缺陷：未收录模型返回负/0。
    assert resolve_context_window("totally-unknown-model-xyz") == 128_000
