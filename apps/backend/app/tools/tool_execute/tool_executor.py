"""工具 handler 隔离执行器：在守护子进程中运行单个工具 handler 并提供硬超时强杀保护。"""

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
        """初始化工具执行器。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建执行器实例，初始化子进程启动标记 ``_process_started`` 为 False；
            不在此处创建任何子进程或持有外部资源。
        """

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
        """在隔离子进程中执行单个工具 handler 并返回归一化结果。

        参数:
            tool: 工具定义，提供 handler、权限、超时等执行契约。
            arguments: 已通过参数校验的关键字参数字典。
            tool_call_id: 关联本次执行的模型工具调用 id，用于回写观察结果。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"，
            失败/超时/异常/启动失败为 status="error"（含 reason 与 retryable）。

        异常:
            ValueError: 当 ``tool.timeout_seconds`` 为 None 时抛出，禁止无限等待；
                此外 ``timeout_seconds<=0`` 会被当作 0（立即超时）处理。

        副作用:
            启动一个守护子进程执行 handler；按 ``timeout_seconds`` 软超时后
            强制 terminate/kill 并清理进程；以 INFO/WARNING/ERROR 级别写入
            执行、超时、失败日志；不修改 ``tool`` 或 ``arguments``。
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
                        "msg": "工具执行超时，已返回超时错误",
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
                    "msg": "工具 handler 执行抛异常",
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
        """管理子进程生命周期，确保退出时清理残留进程。

        参数:
            process: 待托管的子进程对象（已由调用方创建，尚未 start）。

        返回:
            上下文管理器，yield 期间调用方可 start/await 该进程。

        异常:
            透传调用方在 with 块内抛出的异常。

        副作用:
            无论 with 块正常退出还是因超时/异常退出，``finally`` 中若进程
            仍存活则调用 :meth:`_force_kill` 强杀并 join(2) 等待回收；
            ``_process_started`` 标记用于判断是否需要清理。
        """

        try:
            yield
        finally:
            if self._process_started and process.is_alive():
                self._force_kill(process)
                process.join(2)

    def _force_kill(self, process: multiprocessing.Process) -> None:
        """三轮强杀子进程及其可能的孙进程。

        参数:
            process: 需要终止的子进程对象。

        返回:
            无。

        异常:
            不抛出：底层 ``OSError``/``ValueError``（含 PID 已回收、
            ``ProcessLookupError``）均被静默吞掉。

        副作用:
            先 ``terminate()`` 礼貌退出并等待 1s；仍存活则 ``kill()`` 强杀
            再等待 1s；在 POSIX 系统进一步 ``os.killpg(pid, SIGKILL)``
            清理孙进程（子进程已在入口 ``os.setsid()`` 自立为进程组组长，
            故此处能可靠杀掉 handler fork 出的孙进程）。
        """

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
        """把 handler 的任意返回值归一化为 :class:`ToolObservation`。

        参数:
            tool: 工具定义，提供名称与权限，用于构造成功观察。
            payload: handler 实际返回值。
            tool_call_id: 关联模型工具调用的 id。

        返回:
            若 ``payload`` 已是 :class:`ToolObservation` 则透传（缺
            ``tool_call_id`` 时注入），否则用 :func:`tool_success` 包装：
            结构化数据（dict/list）经 ``json.dumps`` 序列化，标量经 ``str()``。

        异常:
            无。

        副作用:
            不修改入参；仅在需要时通过 ``dataclasses.replace`` 生成带
            ``tool_call_id`` 的新 :class:`ToolObservation`。
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
        """子进程入口：执行 handler 并把结果/异常放入结果队列。

        参数:
            handler: 待执行的可调用对象。
            arguments: handler 的关键字参数（已 dict 化以便跨进程序列化）。
            result_queue: 与父进程共享的结果队列，承载
                ``("success"|"error", payload)`` 元组。
            log_queue: 父进程跨进程日志队列；为 None 时子进程退化为默认 logging。

        返回:
            无（结果通过 ``result_queue`` 回传）。

        异常:
            不向上抛出：handler 抛出的任意异常会被捕获并包装为
            ``("error", {"message", "traceback"})`` 放入队列。

        副作用:
            纯静态方法，不访问实例/类状态；POSIX 下进入时调用 ``os.setsid()``
            自立为进程组组长（Windows 跳过），使 :meth:`_force_kill` 的
            ``os.killpg`` 能可靠清理 handler fork 出的孙进程；当 ``log_queue``
            非空时通过 ``install_logging_for_current_process`` 将子进程日志重新接入
            父进程统一管线；执行结束前把成功结果或异常现场写入 ``result_queue``。
        """

        # POSIX 下让子进程自立为进程组组长，使 _force_kill 的 os.killpg
        # 能真正杀掉 handler 可能 fork 出的孙进程；Windows 无 setsid，跳过。
        if os.name == "posix":
            os.setsid()

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
