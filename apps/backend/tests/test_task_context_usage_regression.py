"""task-level context usage: 修复后回归测试（clamp 边界 + 正常路径不受影响）。

本文件只做验证，不修改任何业务代码。针对第一轮对抗测试暴露并已修复的两个缺陷：
1. ``_emit_context_usage`` 对 ``meter.read()`` 返回 None 的保护；
2. ``TaskService.update_context_usage`` 对负 ``used`` 的 clamp。
重点验证「修复未过度生效」（0 不被误伤、clamp 后续正常写入不受污染）。

契约说明（2026-08-17 与 ``ContextUsage.__post_init__`` 校验对齐）：
    ``ContextUsage`` 值对象在构造时即校验 ``used_tokens >= 0``、``total_tokens > 0``，
    非法占用（如负值）在源头抛 ``ValueError``（值对象契约用例见
    ``test_task_context_usage_adversarial.py`` 的 ``ContextUsage`` 值对象层契约一节），
    不再作为合法值进入事件链。因此 ``_emit_context_usage`` 遇到计量器缺陷（``meter.read``
    抛异常）时记 ``context_usage_meter_failed`` 错误日志并跳过事件与回写，不中断模型执行；
    service 层 ``update_context_usage`` 的负数 clamp 仍保留（供直接传负 int 的调用方防御）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config.configuration import build_agent_registry, set_agent_registry
from app.config.settings import Settings
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
    # 阶段 1.5 后 ``TurnService.create_turn`` 经 ``ModelResolverService.resolve``
    # 做 service 期预解析（设计 §6.4 两段式 ①），无 provider + model 行时抛
    # ``ModelNotConfiguredError``。本测试聚焦 context_usage 回归，需为解析器
    # 播种一行可用模型，使 ``_create_task`` 不再被 None 模型拒绝。
    _seed_minimal_provider_and_model()
    yield
    close_service_dependencies()
    reset_service_dependencies()
    Settings.load()


def _seed_minimal_provider_and_model() -> None:
    """为本组测试播种一行 deepseek 厂商 + 一行可用模型（不联网、不调 LLM）。

    参数:
        无。

    返回:
        无。

    异常:
        无（异常由调用方 fixture 捕获；正常路径下 DB 写入不会失败）。

    副作用:
        向 ``providers`` 与 ``models`` 表各插入一行；不输出 ``api_key`` 至日志。
    """

    from app.service.provider.provider_service import ProviderService
    from app.storage.crud.model_entry_crud import ModelEntryCrud

    provider = ProviderService().create_provider(
        name="DeepSeek 测试",
        provider_type="deepseek",
        api_key="sk-test-not-real",
    )
    ModelEntryCrud().create(
        provider_id=provider.provider_id,
        model_name="deepseek/deepseek-v4-flash",
        display_name="deepseek-v4-flash",
        max_context_window=1_000_000,
        supports_thinking=True,
    )


def _create_workspace() -> str:
    workspace = WorkspaceService().create_workspace(
        name="ws_ctx_reg", root_path="H:/ws_ctx_reg",
    )
    return workspace.workspace_id


def _create_task() -> TaskRecord:
    task, _ = TaskService().create_task_with_initial_turn(
        workspace_id=_create_workspace(),
        agent_id="developer",
        input_text="ctx regression task",
        model_name="deepseek/deepseek-v4-flash",
    )
    return task


# 测试目的：0 是合法占用值，clamp 只应针对 <0；验证 0 原样落库且不产生 clamp 告警。
# 可能发现的缺陷：clamp 条件写成 used <= 0，导致 0 被当作异常数据、误发 warning（假告警噪音）。
def test_update_context_usage_zero_not_clamped_and_no_warning(
    storage_stack: Path, caplog
) -> None:
    task = _create_task()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        updated = TaskService().update_context_usage(task.task_id, 0)
    assert updated.context_usage_used == 0
    assert TaskService().get_task(task.task_id).context_usage_used == 0
    assert not any(
        "context_usage_negative_clamped" in r.getMessage() for r in caplog.records
    ), "0 属合法值，不应触发负数 clamp 告警"


# 测试目的：先写非零值再写 0，验证 0 能真正覆盖旧值而非被当作「无更新」跳过。
# 可能发现的缺陷：实现用 `if used:` 之类的真值判断跳过写库，导致 0 无法清零、前端显示陈旧占用。
def test_update_context_usage_zero_overwrites_previous_nonzero(storage_stack: Path) -> None:
    task = _create_task()
    TaskService().update_context_usage(task.task_id, 8888)
    assert TaskService().get_task(task.task_id).context_usage_used == 8888
    updated = TaskService().update_context_usage(task.task_id, 0)
    assert updated.context_usage_used == 0
    assert TaskService().get_task(task.task_id).context_usage_used == 0


# 测试目的：连续 -1 -> 0 -> 5，最终值必须是 5，验证 clamp 分支不污染后续正常写入。
# 可能发现的缺陷：clamp 时改写了共享状态/缓存，或提前 return 导致后续写入被忽略、值粘滞在 0。
def test_update_context_usage_clamp_then_normal_writes_final_value(
    storage_stack: Path, caplog
) -> None:
    task = _create_task()
    t = task.task_id
    with caplog.at_level(logging.WARNING):
        r_neg = TaskService().update_context_usage(t, -1)
    assert r_neg.context_usage_used == 0
    assert any("context_usage_negative_clamped" in r.getMessage() for r in caplog.records)

    r_zero = TaskService().update_context_usage(t, 0)
    assert r_zero.context_usage_used == 0

    r_pos = TaskService().update_context_usage(t, 5)
    assert r_pos.context_usage_used == 5
    assert TaskService().get_task(t).context_usage_used == 5


# 测试目的：clamp 只改内存中的 used，不应把负数或 0 写成其它字段异常；同时验证多次负数写入幂等。
# 可能发现的缺陷：clamp 使用可变默认参数或类属性缓存，第二次负数调用产生非 0 结果。
def test_update_context_usage_repeated_negative_idempotent_zero(storage_stack: Path) -> None:
    task = _create_task()
    t = task.task_id
    for used in (-1, -999, -(10 ** 12)):
        assert TaskService().update_context_usage(t, used).context_usage_used == 0
    assert TaskService().get_task(t).context_usage_used == 0


# 测试目的：修复后正常路径（meter.read 返回有效 usage）仍必须发事件 + 回写 task 真实 token。
# 可能发现的缺陷：新增 usage is None 保护写成 `if not usage`，把 used_tokens=0 的合法 usage 当空跳过。
def test_emit_context_usage_normal_path_still_writes_back(storage_stack: Path) -> None:
    task = _create_task()
    meter = MagicMock()
    meter.read.return_value = ContextUsage(used_tokens=777, total_tokens=8000)
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get:
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="reg_1", task_id=task.task_id)
    meter.read.assert_called_once_with(force=True)
    mock_write.assert_called_once()
    payload = mock_write.call_args[0][1]
    assert payload.used_tokens == 777
    assert payload.total_tokens == 8000
    mock_service.update_context_usage.assert_called_once_with(task.task_id, 777)


# 测试目的：used_tokens=0 的 usage 是「空上下文」的合法读数，必须仍发事件并回写 0。
# 可能发现的缺陷：None 保护误用真值判断（if not usage / if not usage.used_tokens），导致 0 占用被静默丢弃。
def test_emit_context_usage_zero_used_tokens_not_treated_as_empty(
    storage_stack: Path, caplog
) -> None:
    task = _create_task()
    meter = MagicMock()
    meter.read.return_value = ContextUsage(used_tokens=0, total_tokens=8000)
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    caplog.clear()
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.WARNING):
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="reg_2", task_id=task.task_id)
    mock_write.assert_called_once()
    assert mock_write.call_args[0][1].used_tokens == 0
    mock_service.update_context_usage.assert_called_once_with(task.task_id, 0)
    assert not any(
        "context_usage_meter_empty" in r.getMessage() for r in caplog.records
    ), "used_tokens=0 是合法读数，不应判为 meter 空"


# 测试目的：meter.read 返回 None 时，事件与回写都必须跳过，且只记 warning 不抛异常。
# 可能发现的缺陷：保护仅拦了 write_event 却仍调用 update_context_usage，触发 AttributeError。
def test_emit_context_usage_none_usage_skips_persist_and_warns(
    storage_stack: Path, caplog
) -> None:
    meter = MagicMock()
    meter.read.return_value = None
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    caplog.clear()
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.WARNING):
        model_node._emit_context_usage(step_id="reg_3", task_id="whatever")
    mock_write.assert_not_called()
    mock_get.assert_not_called()
    warn_records = [
        r for r in caplog.records if "context_usage_meter_empty" in r.getMessage()
    ]
    assert warn_records, "meter.read 返回 None 应记 context_usage_meter_empty"
    assert all(r.levelno == logging.WARNING for r in warn_records), "应为 WARNING 级别，非 ERROR"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records), "None 属预期边界，不应报 ERROR"


# 测试目的：meter.read 返回 None 后再返回正常 usage，第二次必须正常工作（状态无残留）。
# 可能发现的缺陷：None 分支设置了「已禁用」标志位，导致后续所有步的上下文占用永久丢失。
def test_emit_context_usage_recovers_after_none_read(storage_stack: Path) -> None:
    task = _create_task()
    meter = MagicMock()
    meter.read.side_effect = [None, ContextUsage(used_tokens=321, total_tokens=4096)]
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get:
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="reg_4a", task_id=task.task_id)
        model_node._emit_context_usage(step_id="reg_4b", task_id=task.task_id)
    assert mock_write.call_count == 1
    assert mock_write.call_args[0][1].used_tokens == 321
    mock_service.update_context_usage.assert_called_once_with(task.task_id, 321)


# 测试目的：计量器缺陷（产出非法占用而被 ContextUsage 校验拦截，如负值）不得中断模型节点——
# _emit_context_usage 必须捕获并记 context_usage_meter_failed 错误日志、跳过事件与回写，
# 且后续正常读数不受污染（状态无残留）。
# 可能发现的缺陷：meter.read 抛异常未捕获导致模型步崩溃；或异常分支留下脏状态污染下一次读数。
def test_emit_context_usage_meter_value_error_logs_and_skips(
    storage_stack: Path, caplog
) -> None:
    task = _create_task()
    meter = MagicMock()
    meter.read.side_effect = [
        ValueError("used_tokens must be >= 0"),
        ContextUsage(used_tokens=12, total_tokens=1000),
    ]
    runtime_context = MagicMock()
    runtime_context.usage_meter = meter
    with patch(f"{NODE}._runtime_context", return_value=runtime_context), \
         patch(f"{NODE}.write_event") as mock_write, \
         patch(f"{NODE}.get_task_service") as mock_get, \
         caplog.at_level(logging.ERROR):
        mock_service = MagicMock()
        mock_get.return_value = mock_service
        model_node._emit_context_usage(step_id="reg_5a", task_id=task.task_id)
        model_node._emit_context_usage(step_id="reg_5b", task_id=task.task_id)
    assert mock_write.call_count == 1, "meter 异常读数不应产出事件，第二次正常读数应产出一次"
    assert mock_write.call_args[0][1].used_tokens == 12
    mock_service.update_context_usage.assert_called_once_with(task.task_id, 12)
    error_records = [
        r for r in caplog.records if "context_usage_meter_failed" in r.getMessage()
    ]
    assert error_records, "meter 抛非法占用应记 context_usage_meter_failed 错误日志"
    assert all(r.levelno == logging.ERROR for r in error_records), "计量器缺陷应为 ERROR 级别"


# 测试目的：clamp 发生时不应破坏 updated_at 单调性，前端排序依赖该字段。
# 可能发现的缺陷：clamp 分支走了不同的更新路径而漏更新 updated_at。
def test_update_context_usage_clamp_still_bumps_updated_at(storage_stack: Path) -> None:
    task = _create_task()
    before = TaskService().get_task(task.task_id).updated_at
    clamped = TaskService().update_context_usage(task.task_id, -3)
    assert clamped.context_usage_used == 0
    assert clamped.updated_at >= before


# 测试目的：clamp 不应吞掉 task 不存在的错误，未知任务传负数仍须抛 KeyError。
# 可能发现的缺陷：clamp 前置分支提前 return 假记录，掩盖不存在的 task_id（静默失败）。
def test_update_context_usage_negative_unknown_task_still_raises(storage_stack: Path) -> None:
    with pytest.raises(KeyError):
        TaskService().update_context_usage("task_missing_for_clamp", -5)
