"""工具 handler 隔离执行器：按 ``execution_mode`` 选择隔离策略运行单个工具 handler。

process 路径在守护子进程中执行并提供硬超时强杀保护（含进程组 / Job Object 树杀），
thread 路径在当前调用线程直接执行、无子进程隔离。
"""

import json
import multiprocessing
import os
import queue
import signal
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from typing import Any, cast

from app.config.logging.context.log_context_store import current_log_context
from app.config.logging.logger import log
from app.config.logging.process_bridge import get_log_queue
from app.core.tool.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.execute.windows_job_object import (
    assign_current_process_to_kill_on_close_job,
)
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.models.enums.error_kind import ErrorKind

_POLL_SECONDS = 0.05


class _ToolExecutionCancelled(Exception):
    """Internal signal raised when a process tool should stop for turn cancellation."""


class ToolHandlerRunner:
    """按 ``execution_mode`` 隔离执行单个工具 handler，并归一化为 ``ToolObservation``。

    - ``"process"``：在隔离子进程中运行 handler，提供硬超时保护（超时即强杀进程 + 进程组 /
      Job Object 树杀）；超时配置由 ``ToolRegistry.register`` 在装配期保证为正数。
    - 其余（含默认 ``"thread"``）：在当前调用线程直接运行 handler，无子进程 / Queue / pickle /
      日志桥开销，但**无硬超时强杀**。

    取消语义：观察 Task 级与工具级两类进程内取消信号（见 :meth:`_cancel_sources`），命中即停止
    本次调用并返回 ``cancelled`` 观察；工具级信号只中止本次调用，run 与 Agent 继续执行。
    process 路径在启动前与等待期间轮询；thread 路径只在 handler 执行**前**检查一次——同步
    handler 无法中途打断，跑完后也不推翻其真实结果。

    **本类不改 Transport 状态**：``execute`` 只在强杀完成之后才返回，调用方随后才投影
    终态，确保前端看到的终态表达的是「进程已收尾」而不是「已决定取消」。

    不负责：权限门禁与参数校验（``ToolAccessGate``）、输出预算（``ToolObservationBudget``）。
    """

    def execute(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行单个工具 handler 并返回归一化结果；执行异常归一化为 error 观察，不向上抛出。

        参数:
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次调用的完整执行边界；``tool_call_id`` 用于回写观察结果，并与
                ``task_id`` 一起界定取消检查的范围。

        副作用:
            无论结果如何，退出前释放本次调用的工具级取消信号。
        """

        try:
            if tool.execution_mode == "process":
                return self._execute_in_process(tool, arguments, execution_context)
            return self._execute_in_thread(tool, arguments, execution_context)
        finally:
            tool_call_cancellation_registry.clear(
                execution_context.task_id, execution_context.tool_call_id
            )

    # ------------------------------------------------------------------
    # Process-isolated execution (hard timeout kill)
    # ------------------------------------------------------------------

    def _execute_in_process(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """在隔离子进程中执行 handler：启动前取消不派生进程；等待期取消、超时或通信断裂都在
        ``finally`` 里先强杀子进程树，再返回观察。"""

        tool_call_id = execution_context.tool_call_id
        if self._is_cancelled(execution_context):
            # 已取消时不再派生新子进程，避免白付一次 spawn + 立刻强杀的代价。
            return self._cancelled_observation(tool, execution_context, "在启动子进程前中止")

        result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        # 父侧放弃等待 feeder 线程 flush：子进程已死或已 drain 后不阻塞父进程退出。
        result_queue.cancel_join_thread()
        process = multiprocessing.Process(
            target=ToolHandlerRunner._execute_handler,
            args=(
                tool.handler,
                dict(arguments),
                result_queue,
                execution_context.for_process_execution(),
                # 为 None 表示父进程未启用日志桥，子进程退化为默认 logging。
                get_log_queue(),
                current_log_context(),
            ),
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            return tool_error(
                tool.name,
                f"the tool process could not be started: {exc}. This indicates "
                f"an environment/runtime problem (e.g. cannot spawn a process), "
                f"not a problem with the arguments. Fix the execution environment "
                f"before retrying; the same call will keep failing until the "
                f"environment is repaired.",
                tool_call_id=tool_call_id,
            )

        try:
            status, payload = self._wait_for_result(
                process,
                result_queue,
                tool.timeout_seconds,
                is_cancelled=partial(self._is_cancelled, execution_context),
            )
        except _ToolExecutionCancelled:
            # 观察在此构造，但对外返回发生在 finally 的强杀之后。
            return self._cancelled_observation(tool, execution_context, "执行途中中止")
        except TimeoutError:
            log.warning(
                "tool_execution_timed_out",
                extra={
                    "msg": "工具执行超时，已返回超时错误",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                        "timeout_seconds": tool.timeout_seconds,
                    },
                },
            )
            return tool_error(
                tool.name,
                f"tool timed out after {tool.timeout_seconds} seconds. This "
                f"may be transient (e.g. heavy load or a slow external call), so "
                f"retrying the same call may succeed. If it keeps timing out, "
                f"simplify the task or increase the tool's timeout_seconds.",
                tool_call_id=tool_call_id,
            )
        except (OSError, EOFError) as exc:
            # 子进程崩溃 / 被外部杀死 / 管道断裂时 result_queue.get 抛 EOFError 或
            # BrokenPipeError（不是 queue.Empty），不收口会破坏「不向上抛出」的契约。
            log.error(
                "tool_process_communication_failed",
                extra={
                    "msg": "工具子进程通信异常，已返回错误观察",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                        "error": str(exc),
                    },
                },
            )
            return tool_error(
                tool.name,
                f"the tool process crashed or its communication pipe broke: {exc}. "
                "Inspect the underlying tool failure and change the plan before calling the "
                "tool again.",
                tool_call_id=tool_call_id,
            )
        finally:
            if process.is_alive():
                self._force_kill(process)

        if status == "error":
            log.error(
                "tool_handler_failed",
                extra={
                    "msg": "工具 handler 执行抛异常",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                        "traceback": payload["traceback"],
                    },
                },
            )
            return tool_error(
                tool.name,
                f"the tool handler raised an exception: {payload['message']}. "
                "Inspect the underlying tool failure and change the plan before calling the "
                "tool again.",
                tool_call_id=tool_call_id,
            )

        return self._normalize_result(tool, payload, tool_call_id)

    @staticmethod
    def _wait_for_result(
        process: multiprocessing.Process,
        result_queue: multiprocessing.Queue,
        timeout: float,
        *,
        is_cancelled: Callable[[], bool],
    ) -> tuple[str, Any]:
        """轮询子进程回写的结果。

        四个出口：取消（抛 ``_ToolExecutionCancelled``）、超时且进程仍存活（抛 ``TimeoutError``）、
        拿到结果、进程已退出但无结果（返回 error payload）。强杀由调用方 ``finally`` 负责。

        返回:
            子进程写入的 ``(status, payload)``；进程退出而无结果时为
            ``("error", {"message": ..., "traceback": ""})``。
        """
        deadline = time.monotonic() + timeout
        while True:
            if is_cancelled():
                raise _ToolExecutionCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if process.is_alive():
                    raise TimeoutError()
                break
            try:
                return result_queue.get(timeout=min(_POLL_SECONDS, remaining))
            except queue.Empty:
                if process.is_alive():
                    continue
                # 进程已退出：给最后一次读取窗口，仍无结果则判定无结果。
                try:
                    return result_queue.get(timeout=0.1)
                except queue.Empty:
                    break
        return "error", {"message": "tool process exited without a result", "traceback": ""}

    # ------------------------------------------------------------------
    # In-thread execution (no subprocess, no hard timeout kill)
    # ------------------------------------------------------------------

    def _execute_in_thread(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """在当前调用线程直接执行 handler（无子进程隔离、无硬超时强杀）。

        ``tool.timeout_seconds`` 在此仅作元数据；取消只在 handler 执行前检查，长耗时同步
        handler 执行中途不会被本方法中断。
        """
        if self._is_cancelled(execution_context):
            return self._cancelled_observation(tool, execution_context, "在开始前中止")
        try:
            result = tool.handler(**arguments, execution_context=execution_context)
        except Exception as exc:
            log.error(
                "tool_handler_failed",
                extra={
                    "msg": "工具 handler 执行抛异常",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                        "traceback": traceback.format_exc(),
                    },
                },
            )
            return tool_error(
                tool.name,
                f"the tool handler raised an exception: {exc}. "
                "Inspect the underlying tool failure and change the plan before calling the "
                "tool again.",
                tool_call_id=execution_context.tool_call_id,
            )
        # handler 已跑完即保留真实结果：副作用已经发生，改判为取消会让模型误以为操作未执行；
        # run 级停止交给循环的下一个检查点收敛。
        return self._normalize_result(tool, result, execution_context.tool_call_id)

    # Internal helpers

    @staticmethod
    def _cancel_sources(execution_context: ToolExecutionContext) -> tuple[bool, bool]:
        """返回 ``(Task 级已取消, 本调用已取消)``；每次调用都重新读两个注册表。

        Task 级信号（按 ``task_id``）停止整个 Task 的当前执行；工具级信号（按 ``task_id`` +
        ``tool_call_id``）只中止本次调用。必须现读而不是在执行开始时取一次快照：快照会让工具
        启动之后到达的取消永远查不到。
        """
        return (
            cancellation_registry.is_cancelled(execution_context.task_id),
            tool_call_cancellation_registry.is_cancelled(
                execution_context.task_id, execution_context.tool_call_id
            ),
        )

    @staticmethod
    def _is_cancelled(execution_context: ToolExecutionContext) -> bool:
        return any(ToolHandlerRunner._cancel_sources(execution_context))

    @staticmethod
    def _cancelled_observation(
        tool: ToolDefinition,
        execution_context: ToolExecutionContext,
        stage: str,
    ) -> ToolObservation:
        """记录取消日志并构造统一的取消观察（**不投影 Transport 状态**）。

        三条触达路径（process 启动前、process 等待中、thread 执行前）共用本方法，保证日志与
        观察一致。检出取消时 process 路径的子进程**尚未被强杀**，此刻宣布 ``cancelled`` 等于断言
        一个尚未成立的事实；终态由调用方在 :meth:`execute` 返回之后投影。

        参数:
            stage: 取消发生环节的中文描述，仅进日志，用于排查取消在哪一步被检出。
        """

        run_cancelled, tool_call_cancelled = ToolHandlerRunner._cancel_sources(execution_context)
        log.info(
            "tool_execution_cancelled",
            extra={
                "msg": f"工具执行已中止（{stage}）",
                "data": {
                    "error_kind": ErrorKind.RUNTIME_FAILED.value,
                    "tool_name": tool.name,
                    "tool_call_id": execution_context.tool_call_id,
                    "run_cancelled": run_cancelled,
                    "tool_call_cancelled": tool_call_cancelled,
                },
            },
        )
        return tool_cancelled(
            tool.name,
            tool_call_id=execution_context.tool_call_id,
            reason=(
                "the user stopped this tool call mid-execution — typically because it was too "
                "slow, showed no progress, or was not what they wanted. Its outcome is unknown "
                "and partial side effects are possible; do not re-run it automatically — wait "
                "for the user or propose a smaller, verifiable step."
            ),
        )

    @staticmethod
    def _force_kill(process: multiprocessing.Process) -> None:
        """三轮强杀仍存活的子进程及其孙进程：terminate → kill → POSIX 进程组 SIGKILL。

        子进程已在入口 ``os.setsid()`` 自立为进程组组长，故 ``killpg`` 能可靠清理 handler fork
        出的孙进程；Windows 由子进程入口挂入的 KILL_ON_JOB_CLOSE Job Object 兜底树杀。进程已死或
        PID 已回收属强杀常态，只留 debug 痕迹。
        """

        # 只对已启动且仍存活的进程调用，pid 必然存在。
        pid = cast(int, process.pid)
        try:
            process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(1)
            if os.name == "posix":
                os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError) as exc:
            log.debug(
                "tool_process_force_kill_skipped",
                extra={
                    "msg": "强杀子进程时进程已退出或句柄失效",
                    "data": {"pid": pid, "error": str(exc)},
                },
            )

    @staticmethod
    def _normalize_result(
        tool: ToolDefinition,
        payload: Any,
        tool_call_id: str,
    ) -> ToolObservation:
        """把 handler 返回值归一化为 :class:`ToolObservation`：观察透传（缺 ``tool_call_id`` 时
        注入），dict/list 经 ``json.dumps``，其余标量经 ``str()`` 包成成功观察。"""

        if isinstance(payload, ToolObservation):
            if tool_call_id and not payload.tool_call_id:
                return replace(payload, tool_call_id=tool_call_id)
            return payload

        if isinstance(payload, dict | list):
            content = json.dumps(payload, ensure_ascii=False, default=str)
        else:
            content = str(payload)
        return tool_success(tool.name, content, tool_call_id=tool_call_id)

    # ------------------------------------------------------------------
    # Child-process entry point (static — no self/cls access)
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_handler(
        handler: Callable[..., Any],
        arguments: dict[str, Any],
        result_queue: multiprocessing.Queue,
        execution_context: ToolExecutionContext,
        log_queue: "multiprocessing.Queue | None" = None,
        trace_id: str = "",
    ) -> None:
        """子进程入口：执行 handler 并把 ``("success"|"error", payload)`` 放入结果队列，不向上抛出。

        参数:
            execution_context: 已清空父进程专属对象的执行边界；作为关键字参数注入 handler。
            log_queue: 父进程跨进程日志队列；为 None 时子进程退化为默认 logging。
            trace_id: 父进程启动子进程时的日志链路标识，用于恢复子进程日志上下文。

        副作用:
            POSIX 下 ``os.setsid()`` 自立为进程组组长、Windows 下挂入 KILL_ON_JOB_CLOSE Job
            Object，使父进程强杀能连带清理孙进程。
        """

        if os.name == "posix":
            os.setsid()
        else:
            assign_current_process_to_kill_on_close_job()

        if log_queue is not None:
            # spawn 子进程是全新解释器：导入 configuration 会触发 ``install_msg_relocation()``，
            # 使 ``extra["msg"]`` 约定在子进程同样生效；随后将日志导向父进程队列。
            from app.config.logging.configuration import (
                install_logging_for_current_process,
            )
            from app.config.logging.context.log_context_store import merge_log_context

            install_logging_for_current_process(log_queue=log_queue)
            if trace_id:
                merge_log_context(trace_id=trace_id)

        try:
            result_queue.put(
                (
                    "success",
                    handler(**arguments, execution_context=execution_context),
                )
            )
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
        finally:
            # multiprocessing.Queue 的写入由 daemon feeder 线程异步完成：子进程在 feeder 把缓冲
            # 写入管道前退出会丢数据，父进程表现为随机超时 / "exited without a result"（大
            # payload 时概率显著上升）。close() + join_thread() 等 feeder flush 完再退出。
            result_queue.close()
            result_queue.join_thread()
