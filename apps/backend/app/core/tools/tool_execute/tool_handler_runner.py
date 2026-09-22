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
from threading import Event
from typing import Any

from app.config.logging.context.log_context_store import current_log_context
from app.config.logging.logger import log
from app.config.logging.process_bridge import get_log_queue
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.tools.schemas import OutputSink, ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.schemas.tool_output import ProcessToolOutputChannel
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import handler_exception_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_execute.windows_job_object import (
    assign_current_process_to_kill_on_close_job,
)
from app.models.enums.error_kind import ErrorKind

# 实时输出队列容量上限：满时对子进程施加反压，父进程继续 drain 后再恢复写入。
_OUTPUT_QUEUE_MAXSIZE = 2000
_OUTPUT_QUEUE_DRAIN_TIMEOUT_SECONDS = 5.0
_OUTPUT_DELTA = "delta"
_OUTPUT_COMPLETE = "complete"


class _ToolExecutionCancelled(Exception):
    """Internal signal raised when a process tool should stop for turn cancellation."""


class ToolHandlerRunner:
    """Isolate and execute one tool handler, choosing isolation strategy by ``execution_mode``.

    单一职责：按 ``ToolDefinition.execution_mode`` 选择隔离策略并执行单个工具
    handler，并把成功、失败、超时、异常统一归一化为 ``ToolObservation``：
    - ``"process"``：在隔离的子进程中运行 handler，提供硬超时保护（超时即强杀
      进程 + 进程组 / Job Object 树杀）；
    - 其余（含默认 ``"thread"``）：在当前调用线程直接运行 handler，无子进程 /
      Queue / pickle / 日志桥开销，但**无硬超时强杀**。

    取消语义：执行期间观察 run 级与工具级两类进程内取消信号（见
    :meth:`_build_cancel_check`），命中即停止本次工具调用并返回 ``cancelled`` 观察；
    工具级信号只中止本次调用，run 与 Agent 继续执行。

    **本类不改 Transport 状态**：取消只产出观察与日志，不投影前端。终态一律由执行出口在
    强杀与输出排空完成之后投影（``tool_terminal_projection.project_tool_terminal_state``），
    确保前端看到的终态表达的是「进程已收尾」而不是「已决定取消」。

    职责边界：
    - 负责：按 ``execution_mode`` 分流（thread / process）、子进程隔离执行、
      超时强杀（仅 process 路径：terminate / kill 兜底）、进程组清理、结果归一化、
      取消信号轮询与工具级取消信号的释放。
    - 不负责：权限门禁与参数校验（``ToolAccessGate``）、文件状态协调
      （``FileToolStateCoordinator``）、输出预算（``ToolObservationBudget``）、
      模型可见性策略（``WorkflowOperations``）；thread 路径不提供超时强杀，
      其取消检查只在 handler 执行前 / 执行后两个边界发生。
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext | None = None,
        tool_call_id: str = "",
    ) -> ToolObservation:
        """在隔离子进程或当前线程中执行单个工具 handler 并返回归一化结果。

        按 ``tool.execution_mode`` 分流：
        - ``"process"``：走 :meth:`_execute_in_process`（multiprocessing 子进程 +
          硬超时强杀 + 树杀 + 跨进程日志桥），用于 ``execute_terminal`` 等需 OS 级
          故障隔离的工具；
        - 其余（含默认 ``"thread"``）：走 :meth:`_execute_in_thread`，在当前调用
          线程直接执行 handler，无子进程 / Queue / pickle / 日志桥开销，但**无硬
          超时强杀**。

        参数:
            tool: 工具定义，提供 handler、权限、超时、``execution_mode`` 等执行契约。
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；其
                ``run_id`` 与 ``tool_call_id`` 决定取消检查的范围，见
                :meth:`_build_cancel_check`。
            tool_call_id: 关联本次执行的模型工具调用 id，用于回写观察结果，并作为
                工具级取消信号的键组成部分。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 ``status="success"``，
            启动前 / 执行途中检出取消为 ``status="cancelled"``，失败 / 超时 /
            异常为 ``status="error"``。

        异常:
            不向上抛出：执行异常在分支方法内归一化为 ``status="error"``。

        副作用:
            见各分支方法 docstring；本方法自身只做分流与取消旁路，不直接启动进程或
            线程。取消回调由本方法从 ``cancellation_registry``（run 级）与
            ``tool_call_cancellation_registry``（工具级）现场构造（调用方不传），且
            构造出的是**实时查询**回调而非快照值——见 :meth:`_build_cancel_check`。
            无论结果如何，退出前释放本次调用的工具级取消信号。本方法不投影 Transport
            状态：``cancelled`` 观察交由调用方（执行出口）在进程收尾后统一投影，见
            :meth:`_cancelled_observation` 的时序说明。
        """
        should_cancel = self.build_cancellation_check(execution_context, tool_call_id)
        try:
            if tool.execution_mode == "process":
                return self._execute_in_process(
                    tool,
                    arguments,
                    execution_context,
                    tool_call_id,
                    should_cancel=should_cancel,
                )
            return self._execute_in_thread(
                tool,
                arguments,
                execution_context,
                tool_call_id,
                should_cancel=should_cancel,
            )
        finally:
            # 本次工具调用已结束（任意结果），针对它的工具级信号不再有意义，就地释放；
            # 迟到的信号由 run 收尾时经 ``clear_run`` 兜底回收。
            self.cleanup_cancellation_signal(execution_context, tool_call_id)

    # ------------------------------------------------------------------
    # Process-isolated execution (hard timeout kill)
    # ------------------------------------------------------------------

    def _execute_in_process(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext | None = None,
        tool_call_id: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> ToolObservation:
        """在隔离子进程中执行单个工具 handler 并返回归一化结果。

        参数:
            tool: 工具定义，提供 handler、权限、超时等执行契约。
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                跨进程序列化后由 handler 在执行期消费，便于后续扩展执行参数。
            tool_call_id: 关联本次执行的模型工具调用 id，用于回写观察结果。
            should_cancel: 可选取消检查回调；**启动子进程前**命中则直接返回取消
                观察（不派生进程），**等待结果期间**每轮轮询命中则抛出内部取消信号，
                由本方法强杀子进程后返回取消观察。
            实时输出通道：从 ``execution_context.runtime_dependencies`` 按工具和调用
                身份创建。支持的进程工具自动建立跨进程输出队列；workflow / ToolExecutor
                不再层层传递逐调用 sink。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"，
            失败/超时/异常/启动失败为 status="error"（含 reason 与 retryable），
            取消命中为 status="cancelled"。

        异常:
            不向上抛出：``timeout_seconds`` 为 None / <=0 时直接返回 ``status="error"``
            的 :class:`ToolObservation`（``reason`` 为配置缺失的富文本提示），**不抛**
            ``ValueError``；其余执行异常亦在分支方法内归一化为 ``status="error"``
            （``reason`` 为面向模型的富文本，而非稳定机器短码）。
            调用方若需兜底，应判断返回的 :class:`ToolObservation` 状态，而非捕获异常。

        副作用:
            启动一个守护子进程执行 handler；按 ``timeout_seconds`` 软超时后
            强制 terminate/kill 并清理进程；取消命中时不启动子进程或强杀已启动的
            子进程（含进程组 / Job Object 树杀）；以 INFO/WARNING/ERROR 级别写入
            执行、取消、超时、失败日志；不修改 ``tool`` 或 ``arguments``。
        """

        # --- 1. 防御性地归一化超时参数 ---
        timeout = tool.timeout_seconds
        if timeout is None or timeout <= 0:
            return tool_error(
                tool.name,
                "tool has timeout_seconds=None or timeout_seconds<=0; infinite wait disallowed ",
                reason=(
                    "the tool is misconfigured with timeout_seconds=None or <=0, "
                    "which is not allowed; this is deterministic, so fix the tool's "
                    "timeout configuration before calling it. The same call will "
                    "always fail until the timeout is set."
                ),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        # --- 2. 启动前取消检查：run 已取消时不得再派生新子进程（与 thread 路径
        # 的执行前边界检查对称；避免为已取消的执行白付一次 spawn + 立刻强杀的代价）---
        if should_cancel is not None and should_cancel():
            return self._cancelled_observation(
                tool, tool_call_id, "在启动子进程前中止", execution_context
            )

        # --- 3. 启动隔离子进程 ---
        result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        # 放弃等待 feeder 线程 flush：子进程已死或已 drain 后不阻塞父进程退出
        result_queue.cancel_join_thread()
        # 取父进程已建好的跨进程日志队列；为 None 表示父进程未启用日志桥，
        # 子进程退化为默认 logging（不接入统一管线），不影响工具执行本身。
        log_queue = get_log_queue()
        output_channel = self._create_process_output_channel(
            tool,
            execution_context,
            tool_call_id,
        )
        output_sink: OutputSink | None = None
        if output_channel is not None:

            def _forward_output(text: str) -> None:
                """将执行器回调的文本转交给只消费文本的运行期通道。"""
                output_channel.emit(text)

            output_sink = _forward_output
        output_queue: multiprocessing.Queue | None = None
        if output_channel is not None:
            # 有界队列：队列满时由父进程持续 drain，子进程等待腾出空间后继续发送。
            output_queue = multiprocessing.Queue(maxsize=_OUTPUT_QUEUE_MAXSIZE)
            output_queue.cancel_join_thread()
        process_execution_context = (
            execution_context.for_process_execution()
            if execution_context is None
            else replace(
                execution_context,
                trace_id=execution_context.trace_id or current_log_context(),
            ).for_process_execution()
        )
        process = multiprocessing.Process(
            target=ToolHandlerRunner._execute_handler,
            args=(
                tool.handler,
                dict(arguments),
                result_queue,
                log_queue,
                process_execution_context,
                output_queue,
            ),
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            self._finish_process_output_channel(output_channel)
            return tool_error(
                tool.name,
                str(exc),
                reason=(
                    f"the tool process could not be started: {exc}; this indicates "
                    f"an environment/runtime problem (e.g. cannot spawn a process), "
                    f"not a problem with the arguments. Fix the execution environment "
                    f"before retrying; the same call will keep failing until the "
                    f"environment is repaired."
                ),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        # 进程已启动：进入「等待结果 + 统一清理」分支。等待逻辑收口在
        # :meth:`_wait_for_result`（超时抛 ``TimeoutError``），``finally`` 统一强杀
        # 残留子进程，使清理路径清晰可达、不被静态分析误判为不可达。
        status: str = "error"
        payload: dict[str, Any] = {
            "message": "tool process exited without a result",
            "traceback": "",
        }
        try:
            status, payload = self._wait_for_result(
                process,
                result_queue,
                timeout,
                should_cancel=should_cancel,
                output_queue=output_queue,
                output_sink=output_sink,
            )
        except _ToolExecutionCancelled:
            # 取消观察在此刻构造，但对外返回发生在下方 finally 之后：子进程（及其孙进程）的
            # 强杀与输出排空由 finally 无条件先做完。构造早于收尾本身无害——本层不投影状态，
            # 终态只由执行出口在返回之后投影。
            return self._cancelled_observation(
                tool, tool_call_id, "执行途中中止", execution_context
            )
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
                f"tool timed out after {tool.timeout_seconds} seconds",
                reason=(
                    f"the tool timed out after {tool.timeout_seconds} seconds; this "
                    f"may be transient (e.g. heavy load or a slow external call), so "
                    f"retrying the same call may succeed. If it keeps timing out, "
                    f"simplify the task or increase the tool's timeout_seconds."
                ),
                retryable=True,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )
        except (OSError, EOFError) as exc:
            # 【Bug 修复】子进程崩溃 / 被外部杀死 / 管道断裂时，父进程 result_queue.get
            # 会抛 EOFError 或 OSError(BrokenPipeError)，二者均非 queue.Empty，原代码只
            # 捕获 TimeoutError，会让此类通信异常逃逸出 execute()，破坏 docstring 中
            # "不向上抛出" 的契约，进而可能打断整个 Agent turn。此处归一为
            # handler_exception 错误观察，与已知崩溃语义一致。TimeoutError 是 Exception
            # 子类，必须排在更宽的 OSError/EOFError 之前。
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
                f"tool process communication failed: {exc}",
                reason=handler_exception_reason(
                    f"the tool process crashed or its communication pipe broke: {exc}"
                ),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )
        finally:
            if process.is_alive():
                self._force_kill(process)
                process.join(2)
            self._finish_process_output_channel(output_channel)

        if status == "error":
            log.error(
                "tool_handler_failed",
                extra={
                    "msg": "工具 handler 执行抛异常",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                        "traceback": payload.get("traceback", ""),
                    },
                },
            )
            return tool_error(
                tool.name,
                payload.get("message", "tool handler failed"),
                reason=handler_exception_reason(
                    f"the tool handler raised an exception: "
                    f"{payload.get('message', 'tool handler failed')}"
                ),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        return self._normalize_result(tool, payload, tool_call_id)

    @staticmethod
    def _create_process_output_channel(
        tool: ToolDefinition,
        execution_context: ToolExecutionContext | None,
        tool_call_id: str,
    ) -> ProcessToolOutputChannel | None:
        """Ask the run-scoped factory for an optional live output channel.

        Factory failures disable only the live output side channel; the process tool
        still runs and its ordinary ``ToolObservation`` remains authoritative.
        """
        if execution_context is None:
            return None
        dependencies = execution_context.runtime_dependencies
        factory = dependencies.process_tool_output_channel_factory
        loop = dependencies.runtime_event_loop
        if factory is None or loop is None:
            return None
        try:
            return factory.create(
                task_id=execution_context.task_id,
                run_id=execution_context.run_id,
                tool_call_id=tool_call_id or execution_context.tool_call_id,
                tool_name=tool.name,
                loop=loop,
            )
        except Exception:
            log.exception(
                "tool_output_channel_creation_failed",
                extra={
                    "msg": "创建工具实时输出通道失败，继续执行工具",
                    "data": {"tool_name": tool.name, "tool_call_id": tool_call_id},
                },
            )
            return None

    @staticmethod
    def _finish_process_output_channel(
        output_channel: ProcessToolOutputChannel | None,
    ) -> None:
        """Flush an invocation's live output without changing its tool result."""
        if output_channel is None:
            return
        try:
            output_channel.finish()
        except Exception:
            log.exception(
                "tool_output_channel_finish_failed",
                extra={"msg": "收尾实时输出通道失败，不改变工具执行结果", "data": {}},
            )

    @staticmethod
    def _drain_output_queue(
        output_queue: "multiprocessing.Queue | None",
        output_sink: OutputSink | None,
        *,
        wait_for_completion: bool = False,
    ) -> bool:
        """排空实时输出队列，并可等待子进程明确宣布输出已结束。

        普通模式只 drain 当前可用片段；完成模式有界等待 ``complete`` 控制项，保证
        结果队列先到时也不会让调用方提前清理仍在 flush 的子进程。

        参数:
            output_queue: 跨进程输出队列；为 None 时直接返回。
            output_sink: 片段消费回调；为 None 时直接返回。
            wait_for_completion: 是否等待 ``_execute_handler`` 写入输出完成控制项。

        返回:
            收到输出完成控制项时返回 True，其它情况返回 False。

        异常:
            不向上抛出：队列已关闭 / 管道损坏 / sink 自身抛错，均视为实时通道
            失效只影响实时通道，不影响工具结果的获取与归一化。

        副作用:
            消费队列中的增量与完成控制项，并逐条调用 ``output_sink``。
        """
        if output_queue is None:
            return False
        sink = output_sink
        deadline = (
            time.monotonic() + _OUTPUT_QUEUE_DRAIN_TIMEOUT_SECONDS if wait_for_completion else None
        )
        while True:
            try:
                if deadline is None:
                    item = output_queue.get_nowait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    item = output_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if deadline is not None:
                    continue
                return False
            except Exception:
                # 队列已关闭或管道损坏：实时通道到此为止，最终输出仍由 result_queue
                # 保证。留 debug 痕迹便于事后排查实时通道失效原因，不反压命令执行。
                log.debug(
                    "tool_output_queue_drain_aborted",
                    extra={"msg": "实时输出队列读取异常，结束本轮 drain", "data": {}},
                )
                break
            if not isinstance(item, tuple) or not item:
                log.warning(
                    "tool_output_queue_item_invalid",
                    extra={"msg": "忽略格式错误的实时输出队列项", "data": {}},
                )
                continue
            if item[0] == _OUTPUT_COMPLETE and len(item) == 1:
                return True
            if item[0] != _OUTPUT_DELTA or len(item) != 2:
                log.warning(
                    "tool_output_queue_item_invalid",
                    extra={"msg": "忽略未知的实时输出队列项", "data": {}},
                )
                continue
            _, text = item
            if sink is None:
                continue
            try:
                sink(text)
            except Exception:
                # sink 自身故障不影响工具结果获取；仍须继续 drain，避免子进程在
                # 写入完成标记时因有界队列已满而被挂住。
                log.debug(
                    "tool_output_sink_callback_failed",
                    extra={"msg": "实时输出回调异常，停止推送", "data": {}},
                )
                sink = None
        if wait_for_completion:
            log.warning(
                "tool_output_queue_completion_missing",
                extra={"msg": "等待实时输出完成标记超时，实时通道提前结束", "data": {}},
            )
        return False

    @staticmethod
    def _wait_for_result(
        process: multiprocessing.Process,
        result_queue: multiprocessing.Queue,
        timeout: float,
        should_cancel: Callable[[], bool] | None = None,
        output_queue: "multiprocessing.Queue | None" = None,
        output_sink: OutputSink | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """阻塞轮询子进程回写的执行结果，超时或进程异常退出时给出明确结论。

        参数:
            process: 已 ``start()`` 的守护子进程。
            result_queue: 子进程回写结果的跨进程队列。
            timeout: 软超时秒数（调用方已保证 > 0）。
            should_cancel: 可选取消检查回调；返回 True 时抛出内部取消异常。
            output_queue: 可选跨进程输出队列，承载运行期的输出片段。
            output_sink: 可选片段消费回调，与 ``output_queue`` 成对提供。

        返回:
            ``(status, payload)`` 二元组：成功时为 handler 写入的
            ``("success", ...)`` 或 ``("error", ...)``；进程已死且无结果时返回
            ``("error", {"message": "tool process exited without a result", ...})``。

        异常:
            TimeoutError: 超过 ``timeout`` 且子进程仍存活时抛出，交由调用方
                归一为 ``status="error"``（``reason`` 为超时富文本提示）并在
                finally 中强杀清理。
            _ToolExecutionCancelled: ``should_cancel`` 返回 True 时抛出。

        副作用:
            以 0.05s 步长轮询 ``result_queue``；每轮空转即 drain 一次
            ``output_queue``（实时推送），并在取消 / 成功返回 / 进程退出 / 超时
            四条终态路径上各补一次 drain，保证尾部片段不丢；不修改 ``process``
            状态（强杀由调用方 finally 负责）。
        """
        deadline = time.monotonic() + timeout
        output_complete = False
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                # 取消终态：先收尾已产生的输出，再抛出取消信号。
                output_complete = (
                    ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                    or output_complete
                )
                raise _ToolExecutionCancelled()
            remaining = deadline - time.monotonic()
            try:
                result = result_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                # 空转即实时 drain：这是运行期增量能实时到达前端的关键落点，
                # 缺少此处会退化为命令结束后一次性刷出。
                output_complete = (
                    ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                    or output_complete
                )
                if process.is_alive():
                    continue
                # 进程已退出：再给 0.1s 做最后一次非阻塞读取，仍无结果则判定无结果。
                try:
                    result = result_queue.get(timeout=0.1)
                except queue.Empty:
                    output_complete = (
                        ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                        or output_complete
                    )
                    break
                output_complete = (
                    ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                    or output_complete
                )
                if output_queue is not None and not output_complete:
                    output_complete = ToolHandlerRunner._drain_output_queue(
                        output_queue, output_sink, wait_for_completion=True
                    )
                return result
            # 结果和实时输出使用独立跨进程队列。收到结果后仍须等待输出队列的
            # 完成控制项，确保 handler finally 已排空 feeder 再清理子进程。
            output_complete = (
                ToolHandlerRunner._drain_output_queue(output_queue, output_sink) or output_complete
            )
            if output_queue is not None and not output_complete:
                ToolHandlerRunner._drain_output_queue(
                    output_queue, output_sink, wait_for_completion=True
                )
            return result
        if process.is_alive():
            # 超时终态：强杀前收尾，保留已产生的部分输出。
            output_complete = (
                ToolHandlerRunner._drain_output_queue(output_queue, output_sink) or output_complete
            )
            raise TimeoutError()
        output_complete = (
            ToolHandlerRunner._drain_output_queue(output_queue, output_sink) or output_complete
        )
        return "error", {
            "message": "tool process exited without a result",
            "traceback": "",
        }

    # ------------------------------------------------------------------
    # In-thread execution (no subprocess, no hard timeout kill)
    # ------------------------------------------------------------------

    def _execute_in_thread(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        execution_context: ToolExecutionContext | None,
        tool_call_id: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> ToolObservation:
        """在当前调用线程直接执行 handler 并归一化结果（无子进程隔离）。

        参数:
            tool: 工具定义，提供 handler、权限等执行契约。
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次执行的运行时边界，直接作为关键字参数注入 handler
                （同进程，无需 pickle 序列化）。
            tool_call_id: 关联本次执行的模型工具调用 id。
            should_cancel: 可选取消检查回调；本方法仅在 handler 执行**前**与执行**后**
                两个边界检查，命中即返回取消观察，但**不中止正在执行的同步 handler**
                —— 中途打断交由 process 隔离路径的硬超时强杀负责。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"；handler 抛
            异常为 status="error"；执行前后任一边界检出取消为 status="cancelled"。

        异常:
            不向上抛出：handler 抛出的任意异常被捕获并归一化为
            ``status="error"``（``reason`` 为面向模型的富文本，而非稳定机器短码）。

        副作用:
            在调用方线程内同步执行 handler；直接用主进程 ``log`` 单例；**不启动
            子进程、不建 Queue、不挂载日志桥、不做硬超时强杀**。`execution_mode
            != "process"` 时 ``tool.timeout_seconds`` 仅作元数据，本方法不据此
            监控 / 强杀线程。取消检查仅发生在 handler 调用前后两个边界，长耗时
            同步 handler 执行中途不会被本方法中断。
        """
        # 执行前边界：run 或本次调用已取消则不进入 handler，直接返回取消观察。
        if should_cancel is not None and should_cancel():
            return self._cancelled_observation(
                tool, tool_call_id, "在开始前中止", execution_context
            )
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
                str(exc),
                reason=handler_exception_reason(f"the tool handler raised an exception: {exc}"),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )
        # 执行后边界：handler 已跑完但执行期间被取消，丢弃结果转取消观察。
        if should_cancel is not None and should_cancel():
            return self._cancelled_observation(
                tool, tool_call_id, "在完成后转取消", execution_context
            )
        return self._normalize_result(tool, result, tool_call_id)

    # ------------------------------------------------------------------
    # Public helper contracts
    # ------------------------------------------------------------------

    @staticmethod
    def build_cancellation_check(
        execution_context: ToolExecutionContext | None,
        tool_call_id: str = "",
    ) -> Callable[[], bool] | None:
        """Build the live cancellation check shared by sync and async executors.

        The returned callback reads the run-level and tool-call-level registries on every
        invocation.  Async callers use it to cancel the handler task from a non-blocking
        event-loop watcher; synchronous callers use it at their existing execution
        boundaries and polling points.

        参数:
            execution_context: 当前工具执行上下文；无有效 run id 时返回 ``None``。
            tool_call_id: 当前工具调用 id；为空时回退到上下文中的 id。

        返回:
            实时取消检查回调，或 ``None``（当前执行没有可取消的 run 边界）。

        异常:
            无。

        副作用:
            构造期不读取或修改注册表；回调执行时只读注册表。
        """

        return ToolHandlerRunner._build_cancel_check(execution_context, tool_call_id)

    @staticmethod
    def cleanup_cancellation_signal(
        execution_context: ToolExecutionContext | None,
        tool_call_id: str,
    ) -> None:
        """Clear the tool-call cancellation signal at the execution boundary.

        This is the public cleanup contract for every executor entry point.  It is
        idempotent and only clears the exact ``(run_id, tool_call_id)`` signal; run-level
        cancellation remains untouched.

        参数:
            execution_context: 当前工具执行上下文。
            tool_call_id: 当前工具调用 id；为空时回退到上下文中的 id。

        返回:
            无。

        异常:
            无；注册表清理失败不会由本契约主动抛出。

        副作用:
            从进程内工具调用取消注册表移除当前调用的取消信号。
        """

        ToolHandlerRunner._clear_tool_call_cancellation(execution_context, tool_call_id)

    def normalize_result(
        self,
        tool: ToolDefinition,
        payload: Any,
        tool_call_id: str,
    ) -> ToolObservation:
        """Normalize an in-process handler result for all executor entry points.

        Async handlers stay on the workflow event loop, so they cannot use the full
        synchronous runner dispatch.  This narrow public helper lets ``ToolExecutor``
        reuse the runner's single result-normalization contract without duplicating it.
        """

        return self._normalize_result(tool, payload, tool_call_id)

    # Internal helpers

    @staticmethod
    def _build_cancel_check(
        execution_context: ToolExecutionContext | None,
        tool_call_id: str = "",
    ) -> Callable[[], bool] | None:
        """构造「本次执行是否已被取消」的实时检查回调。

        同时观察两类进程内信号：

        - **run 级取消**（``cancellation_registry``）：整个 run 停止，工具调用一并中止；
        - **工具级取消**（``tool_call_cancellation_registry``）：只中止本次工具调用，
          run 与 Agent 继续执行。

        返回的调用对象每次被调用都**重新读取**两个注册表，因此调用方在轮询 / 边界检查
        间隙里能观察到执行途中发生的取消；若在此处取一次布尔快照再传给分支方法，工具
        启动瞬间之后的取消将永远查不到，取消分支形同死代码。

        参数:
            execution_context: 本次执行的运行时边界；其 ``run_id`` 界定 run 级取消范围。
            tool_call_id: 本次工具调用标识，界定工具级取消范围；为空时回退到
                ``execution_context.tool_call_id``，两者都为空则只观察 run 级信号
                （没有工具调用身份时不构成可取消范围）。

        返回:
            绑定 ``run_id``（以及可能存在的 ``tool_call_id``）的 ``() -> bool`` 取消
            查询回调；``execution_context`` 为 None 或 ``run_id <= 0``（无 run 绑定的
            直接调用）时返回 None，调用方视为「不可取消」。

        异常:
            无（不在构造期读取注册表）。

        副作用:
            无；返回的调用对象被调用时只读注册表，不写任何状态。
        """

        if execution_context is None or execution_context.run_id <= 0:
            return None
        # partial 冻结查询目标（run_id / tool_call_id）而非查询结果：每次调用都重新读注册表。
        run_id = execution_context.run_id
        is_run_cancelled = execution_context.runtime_dependencies.is_run_cancelled
        run_check: Callable[[], bool] = (
            partial(is_run_cancelled, run_id)
            if is_run_cancelled is not None
            else partial(cancellation_registry.is_cancelled, run_id)
        )
        call_id = tool_call_id or execution_context.tool_call_id
        if not call_id:
            return run_check
        tool_check = partial(tool_call_cancellation_registry.is_cancelled, run_id, call_id)
        return lambda: run_check() or tool_check()

    @staticmethod
    def _clear_tool_call_cancellation(
        execution_context: ToolExecutionContext | None,
        tool_call_id: str,
    ) -> None:
        """释放本次工具调用可能被标记的进程内取消信号。

        信号生命周期与调用生命周期绑定：调用结束后信号不再有消费者，就地清理可避免长
        会话在进程内无限累积。清理幂等，未标记过时不做任何事。

        参数:
            execution_context: 本次执行的运行时边界；``run_id <= 0`` 时无键可清。
            tool_call_id: 本次工具调用标识；为空时回退到
                ``execution_context.tool_call_id``。

        返回:
            无。

        异常:
            不抛出：注册表清理是纯内存集合操作，不做 IO、不跨进程，因此不需要在
            ``execute`` 的 finally 里再加防御性兜底。

        副作用:
            从 ``tool_call_cancellation_registry`` 移除 ``(run_id, tool_call_id)``。
        """

        if execution_context is None or execution_context.run_id <= 0:
            return
        call_id = tool_call_id or execution_context.tool_call_id
        if not call_id:
            return
        tool_call_cancellation_registry.clear(execution_context.run_id, call_id)

    @staticmethod
    def _cancellation_sources(
        execution_context: ToolExecutionContext | None,
        tool_call_id: str,
    ) -> tuple[bool, bool]:
        """返回本次中止由哪些进程内取消信号触发，供取消日志定位来源。

        调用方只应在**已确定取消**的路径上调用：正常执行路径不应调用本方法，以免在
        高频轮询里多读一次注册表。

        run 级判定优先使用注入的查询入口（与 :meth:`_build_cancel_check` 的取消判定同源），
        未注入时才回落到进程内单例；否则注入替身时日志记录的来源会与实际判定不一致。

        参数:
            execution_context: 本次执行的运行时边界；提供 run 绑定与工具调用身份。
            tool_call_id: 本次工具调用标识；为空时回退到
                ``execution_context.tool_call_id``。

        返回:
            ``(run_cancelled, tool_call_cancelled)``：无 run 绑定时两者均为 False；
            工具调用身份缺失时 ``tool_call_cancelled`` 恒为 False。

        异常:
            无。

        副作用:
            无；只读取消查询入口与工具级注册表。
        """

        if execution_context is None or execution_context.run_id <= 0:
            return False, False
        call_id = tool_call_id or execution_context.tool_call_id
        is_run_cancelled = execution_context.runtime_dependencies.is_run_cancelled
        run_cancelled = (
            is_run_cancelled(execution_context.run_id)
            if is_run_cancelled is not None
            else cancellation_registry.is_cancelled(execution_context.run_id)
        )
        return (
            run_cancelled,
            tool_call_cancellation_registry.is_cancelled(execution_context.run_id, call_id),
        )

    @staticmethod
    def _cancelled_observation(
        tool: ToolDefinition,
        tool_call_id: str,
        stage: str,
        execution_context: ToolExecutionContext | None,
    ) -> ToolObservation:
        """记录取消日志并构造统一的取消观察（**不投影 Transport 状态**）。

        取消有四条触达路径（process 启动前、process 等待中、thread 执行前、thread
        执行后），四处的日志与观察必须完全一致，避免只改一处的行为漂移。

        时序：本方法在检出取消的瞬间即被调用，此时 process 路径的子进程**尚未被强杀**，
        因此这里不得变更前端状态——那时宣布 ``cancelled`` 等于断言一个尚未成立的事实
        （进程可能仍在写文件、仍在产出输出），而且会让强杀期间排空出来的输出增量因
        ``ToolCallRuntimeUpdateEvent`` 要求 ``part.status == "running"`` 而被丢弃。终态由
        执行出口在本方法返回、且强杀与输出排空完成之后统一投影
        （``tool_terminal_projection.project_tool_terminal_state``）。

        参数:
            tool: 被取消的工具定义，提供工具名与权限。
            tool_call_id: 关联本次执行的模型工具调用 id。
            stage: 取消发生环节的中文描述（如「在开始前中止」），仅进日志，用于
                排查取消是在哪一步被检出。
            execution_context: 本次执行的运行时边界，提供 task_id / run_id，仅用于日志中的
                取消来源判定；为 None 或未绑定 run 时来源记为未取消。

        返回:
            ``status="cancelled"``、统一 ``reason`` 的 :class:`ToolObservation`。

        异常:
            无。

        副作用:
            以 INFO 级写入一条含取消来源标记的 ``tool_execution_cancelled`` 日志；不写
            Transport snapshot、不落库、不写模型上下文。
        """

        run_cancelled, tool_call_cancelled = ToolHandlerRunner._cancellation_sources(
            execution_context, tool_call_id
        )
        log.info(
            "tool_execution_cancelled",
            extra={
                "msg": f"工具执行已中止（{stage}）",
                "data": {
                    "error_kind": ErrorKind.RUNTIME_FAILED.value,
                    "tool_name": tool.name,
                    "tool_call_id": tool_call_id,
                    "run_cancelled": run_cancelled,
                    "tool_call_cancelled": tool_call_cancelled,
                },
            },
        )
        return tool_cancelled(
            tool.name,
            permission=tool.permission,
            tool_call_id=tool_call_id,
            reason=(
                "the user stopped this tool call mid-execution — typically because it was too "
                "slow, showed no progress, or was not what they wanted. Its outcome is unknown "
                "and partial side effects are possible; do not re-run it automatically — wait "
                "for the user or propose a smaller, verifiable step."
            ),
        )

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
                kill_process_group = getattr(os, "killpg", None)
                kill_signal = getattr(signal, "SIGKILL", None)
                if kill_process_group is not None and kill_signal is not None:
                    kill_process_group(pid, kill_signal)
            # Windows: TerminateProcess 杀进程树需要内核句柄，此处不额外做
        except (OSError, ValueError) as exc:
            # 进程已死或 PID 已回收属强杀常态，但留 debug 痕迹以便排查树杀残留。
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
        return tool_success(
            tool.name,
            tool.permission,
            content,
            tool_call_id=tool_call_id,
        )

    # ------------------------------------------------------------------
    # Child-process entry point (static — no self/cls access)
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_handler(
        handler: Callable[..., Any],
        arguments: dict[str, Any],
        result_queue: multiprocessing.Queue,
        log_queue: "multiprocessing.Queue | None" = None,
        execution_context: ToolExecutionContext | None = None,
        output_queue: "multiprocessing.Queue | None" = None,
    ) -> None:
        """子进程入口：执行 handler 并把结果/异常放入结果队列。

        参数:
            handler: 待执行的可调用对象。
            arguments: handler 的关键字参数（已 dict 化以便跨进程序列化）。
            result_queue: 与父进程共享的结果队列，承载
                ``("success"|"error", payload)`` 元组。
            log_queue: 父进程跨进程日志队列；为 None 时子进程退化为默认 logging。
            execution_context: 本次执行的运行时边界；随 ``arguments`` 一同跨进程序列化，
                作为关键字参数 ``execution_context`` 注入 handler。其 ``trace_id`` 用于
                恢复子进程日志的链路上下文。
            output_queue: 可选实时输出队列；非 None 时以关键字参数 ``output_sink``
                注入 handler，handler 可在运行期回传原始文本片段；子进程完成前会追加
                完成控制项，父进程据此排空队列。

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
            父进程统一管线；执行结束前把成功结果或异常现场写入 ``result_queue``；
            当 ``output_queue`` 非空时在 handler 运行期向其写入输出片段。
        """

        # POSIX 下让子进程自立为进程组组长，使 _force_kill 的 os.killpg
        # 能真正杀掉 handler 可能 fork 出的孙进程；Windows 无 setsid，跳过。
        if os.name == "posix":
            create_process_group = getattr(os, "setsid", None)
            if create_process_group is not None:
                create_process_group()
        else:
            # Windows：把子进程自身挂入 KILL_ON_JOB_CLOSE 的 Job Object，使父进程
            # _force_kill 强杀该子进程时，其 shell 孙进程被 OS 级树杀兜底。
            assign_current_process_to_kill_on_close_job()

        if log_queue is not None:
            # spawn 子进程是全新解释器：导入 configuration 会触发
            # ``app.config.logging`` 包的 ``install_msg_relocation()``，
            # 使规范约定的 ``extra["msg"]`` 在子进程同样生效；随后将日志
            # 导向父进程队列，复用父进程已配好的截断管线。
            from app.config.logging.configuration import (
                install_logging_for_current_process,
            )
            from app.config.logging.context.log_context_store import merge_log_context

            install_logging_for_current_process(log_queue=log_queue)
            if execution_context is not None and execution_context.trace_id:
                merge_log_context(trace_id=execution_context.trace_id)

        extra_kwargs: dict[str, Any] = {}
        if output_queue is not None:
            output_incomplete = Event()

            def _output_sink(text: str) -> None:
                """无损回传一段输出片段；队列满时等待父进程 drain。

                参数:
                    text: 原样的增量输出片段。

                返回:
                    无。

                异常:
                    不向上抛出：队列关闭或管道损坏时标记实时输出不完整；队列满只会
                    等待父进程消费，不会丢弃片段。

                副作用:
                    向跨进程 ``output_queue`` 写入一条 delta 项；有界队列通过反压控制
                    在途内存，不设置字符预算或静默丢弃策略。
                """
                if output_incomplete.is_set():
                    return
                try:
                    output_queue.put((_OUTPUT_DELTA, text))
                except Exception:
                    output_incomplete.set()

            extra_kwargs["output_sink"] = _output_sink

        try:
            result_queue.put(
                (
                    "success",
                    handler(**arguments, execution_context=execution_context, **extra_kwargs),
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
            if output_queue is not None:
                # result_queue 与 output_queue 相互独立；父进程收到结果后会持续 drain
                # 实时队列，直到读到完成控制项。队列满时数据写入反压 handler，避免丢片段。
                try:
                    output_queue.put(
                        (_OUTPUT_COMPLETE,),
                        timeout=_OUTPUT_QUEUE_DRAIN_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    log.debug(
                        "tool_output_queue_completion_failed",
                        extra={"msg": "实时输出完成控制项投递失败", "data": {"error": str(exc)}},
                    )
                try:
                    output_queue.close()
                    output_queue.join_thread()
                except Exception as exc:
                    log.debug(
                        "tool_output_queue_flush_failed",
                        extra={"msg": "实时输出队列关闭或 flush 失败", "data": {"error": str(exc)}},
                    )
                if output_incomplete.is_set():
                    log.warning(
                        "tool_output_transport_incomplete",
                        extra={"msg": "实时输出队列传输失败，已投递不完整标记", "data": {}},
                    )
            # 【Bug 修复】multiprocessing.Queue 的写入由一条 daemon feeder 线程异步完成，
            # put() 仅把数据塞进进程内缓冲并唤醒 feeder。若子进程在 feeder 把缓冲写入
            # 管道前就因 return 退出，daemon 线程会被强制中止、缓冲数据直接丢失，父进程
            # 永远 get 不到结果（表现为随机超时 / "exited without a result"，大 payload 如
            # 整文件读取、长终端输出时概率显著上升）。
            # close() 标记队列不再写入，join_thread() 阻塞等待 feeder 把剩余缓冲全部 flush
            # 到管道后再退出进程，确保父进程稳定读到结果。此父侧 cancel_join_thread() 仅
            # 影响父进程退出时不阻塞，与子进程 flush 无关，不冲突。
            result_queue.close()
            result_queue.join_thread()
