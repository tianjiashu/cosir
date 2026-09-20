"""跨测试文件的共享夹具：隔离后端日志的全局配置。

本模块只做一件事——保证同进程内日志的全局开关在用例之间不互相污染。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

# 会被 ``configure_logging`` 改写 ``propagate`` / ``level`` 的后端 logger。
_BACKEND_LOGGERS = ("coding_agent.backend", "uvicorn.error")

# 单个 logger 需要复原的全局开关：``level`` / ``propagate`` / ``disabled``。
_LoggerState = tuple[int, bool, bool]


def _snapshot() -> tuple[dict[str, _LoggerState], int]:
    """快照后端 logger 的全局开关与全局禁用阈值。

    参数:
        无。

    返回:
        ``({logger_name: (level, propagate, disabled)}, manager.disable)``。``manager.disable``
        是 ``logging.disable()`` 写入的全局阈值，非 0 时会让低于该级别的记录被整体丢弃。

    异常:
        无。

    副作用:
        无（只读取全局配置）。
    """

    states: dict[str, _LoggerState] = {}
    for name in _BACKEND_LOGGERS:
        logger = logging.getLogger(name)
        states[name] = (logger.level, logger.propagate, logger.disabled)
    return states, logging.root.manager.disable


def _restore(snapshot: tuple[dict[str, _LoggerState], int]) -> None:
    """把后端 logger 的全局开关与全局禁用阈值复原到快照值。

    参数:
        snapshot: :func:`_snapshot` 产出的快照。

    返回:
        无。

    异常:
        无。

    副作用:
        改写 :data:`_BACKEND_LOGGERS` 中每个 logger 的 ``level`` / ``propagate`` / ``disabled``，
        并复原 ``logging.disable()`` 的全局阈值；不增删 handler，也不改动 ``Logger.manager``。
    """

    states, root_disable = snapshot
    for name, (level, propagate, disabled) in states.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
    logging.disable(root_disable)


@pytest.fixture(autouse=True)
def restore_backend_logging_state() -> Iterator[None]:
    """在每个用例前后复原日志的全局配置，消除跨文件的日志捕获污染。

    背景：``configure_logging`` 会把 ``coding_agent.backend.propagate`` 置为 ``False``，而
    ``shutdown_logging`` 只移除自己安装的 handler、不复原该开关；用例若再碰上
    ``logger.disabled`` 或 ``logging.disable()``，同进程内后续所有依赖日志记录的 ``caplog``
    断言都会静默抓不到记录，表现为「事件没写」的假失败（与业务行为无关）。

    参数:
        无（pytest 自动注入）。

    返回:
        供 pytest 使用的 ``None`` 迭代器（夹具上下文）。

    异常:
        无。

    副作用:
        用例结束后把 :data:`_BACKEND_LOGGERS` 的 ``level`` / ``propagate`` / ``disabled`` 与
        ``logging.disable()`` 阈值复原为用例开始前的值；不影响用例内部的日志行为（用例内自己
        改的开关在**同一用例内**仍然生效），也不触碰业务代码。
    """

    snapshot = _snapshot()
    try:
        yield
    finally:
        _restore(snapshot)
