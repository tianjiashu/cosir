"""Tool handler executor."""

import json
import multiprocessing
import os
import queue
import signal
import traceback
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from app.config.logging.logger import log
from app.config.logging.process_bridge import get_log_queue
from app.tools.schemas import ToolDefinition, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_success import tool_success


class ToolExecutor:
    """Isolate and execute one tool handler with hard-timeout protection.

    单一职责：在隔离的子进程中运行单个工具 handler，提供硬超时保护
    （超时即强杀进程），并把成功、失败、超时、异常统一归一化为
    ``ToolObservation``。

    职责边界：
    - 负责：子进程隔离执行、超时强杀（terminate/kill 兜底）、进程组清理、
      结果归一化。
    - 不负责：权限校验（``ToolScheduler``）、参数校验（``validation``）、
      模型可见性策略（``ToolExecutionService``）。
    """

    def __init__(self) -> None:
        """Initialize the executor."""

        self._process_started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> ToolObservation:
        """Execute one tool handler with a timeout.

        Args:
            tool:        The tool definition containing handler, timeout, etc.
            arguments:   Validated keyword arguments for the handler.
            tool_call_id:Optional ID to correlate this execution with a model
                         tool call.

        Raises:
            ValueError: If ``tool.timeout_seconds`` is ``None`` (disallowed).
        """

        # --- 1. 防御性地归一化超时参数 ---
        timeout = tool.timeout_seconds
        if timeout is None:
            raise ValueError(f"tool {tool.name} has timeout_seconds=None; infinite wait disallowed")
        if timeout <= 0:
            timeout = 0.0

        # --- 2. 启动隔离子进程 ---
        result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        # 放弃等待 feeder 线程 flush：子进程已死或已 drain 后不阻塞父进程退出
        result_queue.cancel_join_thread()
        # 取父进程已建好的跨进程日志队列；为 None 表示父进程未启用日志桥，
        # 子进程退化为默认 logging（不接入统一管线），不影响工具执行本身。
        log_queue = get_log_queue()
        process = multiprocessing.Process(
            target=ToolExecutor._execute_handler,
            args=(tool.handler, dict(arguments), result_queue, log_queue),
            daemon=True,
        )
        with self._managed_subprocess(process):
            try:
                process.start()
                self._process_started = True
            except Exception as exc:
                return tool_error(
                    tool.name,
                    str(exc),
                    reason="handler_start_failed",
                    retryable=False,
                    permission=tool.permission,
                    tool_call_id=tool_call_id,
                )

            # --- 3. 等待 + 超时判断 ---
            process.join(timeout)
            if process.is_alive():
                log.warning(
                    "tool_execution_timed_out",
                    extra={
                        "message_text": "tool execution timed out",
                        "data": {
                            "tool_name": tool.name,
                            "timeout_seconds": tool.timeout_seconds,
                        },
                    },
                )
                return tool_error(
                    tool.name,
                    f"tool timed out after {tool.timeout_seconds} seconds",
                    reason="timeout",
                    retryable=True,
                    permission=tool.permission,
                    tool_call_id=tool_call_id,
                )

            # --- 4. 取结果 ---
            try:
                status, payload = result_queue.get(timeout=1)
            except queue.Empty:
                status, payload = (
                    "error",
                    {
                        "message": "tool process exited without a result",
                        "traceback": "",
                    },
                )

            # --- 5. 归一化返回 ---
        if status == "error":
            log.error(
                "tool_handler_failed",
                extra={
                    "message_text": "tool handler failed",
                    "data": {
                        "tool_name": tool.name,
                        "traceback": payload.get("traceback", ""),
                    },
                },
            )
            return tool_error(
                tool.name,
                payload.get("message", "tool handler failed"),
                reason="handler_exception",
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        return self._normalize_result(tool, payload, tool_call_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _managed_subprocess(self, process: multiprocessing.Process) -> Any:
        """Ensure the subprocess is cleaned up on exit.

        The ``finally`` block handles process cleanup when the caller exits
        the ``with`` block — either normally (process already exited, no-op)
        or via timeout / exception (process still alive → force kill).
        """

        try:
            yield
        finally:
            if self._process_started and process.is_alive():
                self._force_kill(process)
                process.join(2)

    def _force_kill(self, process: multiprocessing.Process) -> None:
        """Kill a subprocess and its process group."""

        pid = process.pid
        if pid is None:
            return

        try:
            # 第一轮：礼貌 terminate
            process.terminate()
            process.join(1)

            # 第二轮：仍活着 → 强制 kill
            if process.is_alive():
                process.kill()
                process.join(1)

            # 第三轮：杀进程组（清理 handler 可能 fork 的孙进程）
            if os.name == "posix":
                os.killpg(pid, signal.SIGKILL)
            # Windows: TerminateProcess 杀进程树需要内核句柄，此处不额外做
        except (OSError, ValueError):
            pass  # 进程已死或 PID 已回收

    @staticmethod
    def _normalize_result(
        tool: ToolDefinition,
        payload: Any,
        tool_call_id: str,
    ) -> ToolObservation:
        """Normalize arbitrary handler return into ``ToolObservation``.

        - If the handler returns a ``ToolObservation`` directly, pass through
          (injecting ``tool_call_id`` if missing).
        - Otherwise wrap with ``tool_success``, using ``json.dumps`` for
          structured results (dict / list) and ``str()`` for scalars.
        """

        if isinstance(payload, ToolObservation):
            if tool_call_id and not payload.tool_call_id:
                return replace(payload, tool_call_id=tool_call_id)
            return payload

        if isinstance(payload, dict | list):
            content = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            content = str(payload)
        return tool_success(tool, content, tool_call_id=tool_call_id)

    # ------------------------------------------------------------------
    # Child-process entry point (static — no self/cls access)
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_handler(
        handler: Callable[..., Any],
        arguments: dict[str, Any],
        result_queue: multiprocessing.Queue,
        log_queue: "multiprocessing.Queue | None" = None,
    ) -> None:
        """Run a tool handler inside an isolated child process.

        The child process inherits a copy of the parent address space;
        this function must remain a pure static method that only uses
        its arguments — no instance/class state. When ``log_queue`` is
        provided (parent process logging bridge enabled), the child
        re-attaches its ``coding_agent.backend`` logger to the cross-process
        queue so its logs flow into the parent's unified pipeline.
        """

        if log_queue is not None:
            # spawn 子进程是全新解释器：导入 configuration 会触发
            # ``app.config.logging`` 包的 ``install_msg_relocation()``，
            # 使规范约定的 ``extra["msg"]`` 在子进程同样生效；随后将日志
            # 导向父进程队列，复用父进程已配好的脱敏/截断/上下文关联。
            from app.config.logging.configuration import (
                install_logging_for_current_process,
            )

            install_logging_for_current_process(log_queue=log_queue)

        try:
            result_queue.put(("success", handler(**arguments)))
        except Exception as exc:
            result_queue.put(
                (
                    "error",
                    {
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
            )
