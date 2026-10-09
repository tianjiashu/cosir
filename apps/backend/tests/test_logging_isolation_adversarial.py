"""对抗测试：后端 logger 跨用例隔离。

``conftest`` 的 autouse 夹具负责复原 ``coding_agent.backend`` / ``uvicorn.error`` 的
``level`` / ``propagate`` / ``disabled`` 与 ``logging.disable()`` 阈值。本模块证明它确实生效，
并攻击它可能的绕过面（同用例内不得越界复原、``disabled`` 与全局阈值都要覆盖）。

历史：本文件原先还含一组针对 ``ToolCallLifecycleManager.fail_invalid_tools`` 的契约用例；该能力
已从模块中移除（非法调用现在由 ``classify`` 归属到 ``valid_calls`` / ``blocked_calls``，再由执行层
门禁产出可读拒绝、经 ``settle`` 闭合模型协议），故该组用例随能力一并删除。
"""

from __future__ import annotations

import logging

import pytest

from app.config.logging.configuration import configure_logging, shutdown_logging
from app.config.logging.logger import log as backend_log

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
    """A6（绕过面）：``logger.disabled`` 也必须被夹具覆盖。

    若夹具只复原 level/propagate，则把 logger.disabled 置 True 的用例会让后续 caplog 失空。
    """

    logger = logging.getLogger(BACKEND)
    # 该用例自身先恢复 disabled，避免真的污染后续；此处只断言 disabled 语义本身。
    logger.disabled = True
    try:
        with caplog.at_level(logging.DEBUG, logger=BACKEND):
            logger.debug("disabled-probe")
        captured = any("disabled-probe" in r.message for r in caplog.records)
        assert captured is False, "logger.disabled=True 时仍抓到了记录，前置假设不成立"
    finally:
        logger.disabled = False


def test_bypass_logging_disable(caplog: pytest.LogCaptureFixture):
    """A7（绕过面）：``logging.disable()`` 全局禁用同样必须被夹具覆盖。"""

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
