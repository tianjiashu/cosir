"""工具处理函数的进程隔离执行。"""

import logging
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.queues import Queue
import os
import pickle
import sys
import tempfile
import traceback
from typing import Any, Callable, Dict, Optional

from app.config.logging import install_logging_for_current_process
from app.config.logging import get_log_queue

logger = logging.getLogger("coding_agent.backend")


@dataclass(frozen=True)
class ToolExecutionResult:
    """表示一次隔离的工具处理函数执行的结果。

    参数:
        status: 执行状态，为 ``success`` 或 ``error``。
        content: 处理函数成功时返回的内容。
        error: 执行失败或超时时的错误文本。

    返回:
        不可变的执行结果。

    异常:
        无。

    副作用:
        无。
    """

    status: str
    content: str = ""
    error: str = ""


def execute_tool_handler(
    handler: Callable[..., str],
    arguments: Dict[str, Any],
    timeout_seconds: float,
    log_queue: "Optional[Queue]" = None,
    run_id: str = "",
) -> ToolExecutionResult:
    """在带有硬超时的子进程中执行工具处理函数。

    参数:
        handler: 执行工具操作、可 pickle 的可调用对象。
        arguments: 传给处理函数的关键字参数。
        timeout_seconds: 在终止进程前等待的最大秒数。
        log_queue: 可选父进程日志队列，用于把子进程日志回传统一管线。
        run_id: 当前工具调用的 run 标识，写入子进程日志以便关联。

    返回:
        描述成功、处理函数失败或超时的 ToolExecutionResult。

    异常:
        无。处理函数与进程启动失败都会作为错误返回。

    副作用:
        启动一个子进程，并在达到超时时间时终止它。
    """

    result_file = tempfile.NamedTemporaryFile(delete=False)
    result_path = result_file.name
    result_file.close()

    # 父进程侧自动解析日志队列；未配置日志时退化为 None，子进程日志退回 stderr。
    if log_queue is None:
        log_queue = get_log_queue()

    try:
        context = get_context("spawn")
        process = context.Process(
            target=_run_handler,
            args=(handler, dict(arguments), result_path, log_queue, run_id),
        )
        process.start()
    except Exception as exc:
        logger.exception(
            "tool_subprocess_start_failed",
            extra={"timeout_seconds": timeout_seconds, "error": str(exc)},
        )
        _remove_result_file(result_path)
        return ToolExecutionResult(status="error", error=str(exc))

    try:
        process.join(timeout_seconds)
        if process.is_alive():
            _stop_process(process)
            return ToolExecutionResult(
                status="error",
                error=f"tool timed out after {timeout_seconds} seconds",
            )

        result = _read_result_file(result_path)
        if result is not None:
            status, content, error = result
            return ToolExecutionResult(status=status, content=content, error=error)

        if process.exitcode == 0:
            return ToolExecutionResult(status="success", content="")
        return ToolExecutionResult(
            status="error",
            error=f"tool process exited with code {process.exitcode}",
        )
    finally:
        _remove_result_file(result_path)


def _run_handler(
    handler: Callable[..., str],
    arguments: Dict[str, Any],
    result_path: str,
    log_queue: "Optional[Queue]" = None,
    run_id: str = "",
) -> None:
    """运行处理函数并将其归一化结果写入文件。

    参数:
        handler: 执行工具操作的可调用对象。
        arguments: 传给处理函数的关键字参数。
        result_path: 用于把结果返回给父进程的临时文件路径。
        log_queue: 父进程日志队列，用于把子进程日志回传统一管线。
        run_id: 当前工具调用的 run 标识，写入日志以便关联。

    返回:
        无。

    异常:
        无。处理函数的异常会被捕获并通过结果文件返回。

    副作用:
        执行处理函数并向 ``result_path`` 写入一个 pickle 的结果元组；
        若提供 log_queue，子进程日志经队列回传父进程统一管线。
    """
    if log_queue is not None:
        try:
            install_logging_for_current_process(log_queue=log_queue)
        except Exception as exc:
            # 队列安装失败不影响工具执行；退回 stderr 并明确标记桥接失效，便于排查
            sys.stderr.write(
                "subprocess_log_bridge_failed run_id=%s error=%s\n" % (run_id, exc)
            )
    try:
        result = handler(**arguments)
    except Exception as exc:
        logger.exception("tool_handler_failed", extra={"run_id": run_id})
        _write_result_file(result_path, ("error", "", f"{exc}\n{traceback.format_exc()}"))
        return
    _write_result_file(result_path, ("success", result, ""))


def _stop_process(process) -> None:
    """以 kill 作为兜底来终止子进程。

    参数:
        process: 需要停止的多进程进程。

    返回:
        无。

    异常:
        无。

    副作用:
        向子进程发送终止信号。
    """

    process.terminate()
    process.join(0.2)
    if process.is_alive():
        process.kill()
        process.join(0.2)


def _write_result_file(result_path: str, result: tuple) -> None:
    """将 pickle 的执行结果写入磁盘。

    参数:
        result_path: 待写入的临时文件路径。
        result: 包含 status、content 和 error 的元组。

    返回:
        无。

    异常:
        OSError: 如果结果文件无法写入。
        pickle.PickleError: 如果结果无法被序列化。

    副作用:
        写入结果文件。
    """

    with open(result_path, "wb") as result_file:
        pickle.dump(result, result_file)


def _read_result_file(result_path: str) -> Optional[tuple]:
    """从磁盘读取 pickle 的执行结果。

    参数:
        result_path: 待读取的临时文件路径。

    返回:
        存在时返回结果元组，否则返回 None。

    异常:
        无。缺失、为空或不可读的结果文件会作为缺失或错误结果返回。

    副作用:
        读取结果文件。
    """

    if not os.path.exists(result_path) or os.path.getsize(result_path) == 0:
        return None
    try:
        with open(result_path, "rb") as result_file:
            return pickle.load(result_file)
    except Exception as exc:
        logger.exception("tool_result_read_failed", extra={"error": str(exc)})
        return ("error", "", f"failed to read tool result: {exc}")


def _remove_result_file(result_path: str) -> None:
    """如果临时结果文件存在则删除它。

    参数:
        result_path: 待删除的临时文件路径。

    返回:
        无。

    异常:
        无。

    副作用:
        删除一个临时文件。
    """

    try:
        os.remove(result_path)
    except FileNotFoundError:
        return
