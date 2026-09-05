"""工具 handler 隔离执行器：在守护子进程中运行单个工具 handler 并提供硬超时强杀保护。"""

import json
import multiprocessing
import os
import queue
import signal
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from app.config.logging.logger import log
from app.config.logging.process_bridge import get_log_queue
from app.core.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_error import handler_exception_reason, tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_execute.windows_job_object import (
    assign_current_process_to_kill_on_close_job,
)
from app.core.tools.tool_handler.terminal import OutputSink
from app.models.enums.error_kind import ErrorKind

# 实时输出队列容量上限：满时子进程侧丢弃新片段而非阻塞命令执行。
_OUTPUT_QUEUE_MAXSIZE = 2000


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

    职责边界：
    - 负责：按 ``execution_mode`` 分流（thread / process）、子进程隔离执行、
      超时强杀（仅 process 路径：terminate / kill 兜底）、进程组清理、结果归一化。
    - 不负责：权限门禁与参数校验（``ToolAccessGate``）、文件状态协调
      （``FileToolStateCoordinator``）、输出预算（``ToolObservationBudget``）、
      模型可见性策略（``WorkflowOperations``）；thread 路径不提供超时强杀。
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
        should_cancel: Callable[[], bool] | None = None,
        output_sink: OutputSink | None = None,
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
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）。
            tool_call_id: 关联本次执行的模型工具调用 id，用于回写观察结果。
            should_cancel: 可选取消检查回调；process 模式等待结果时会轮询该回调。
            output_sink: 可选实时输出回调，签名 ``(text, truncated) -> None``。
                **仅 process 模式支持**：父进程轮询跨进程队列后在调用线程内回调它；
                thread 模式忽略该参数（当前无流式产出的 thread 工具）。

        返回:
            归一化后的 :class:`ToolObservation`。

        异常:
            不向上抛出：执行异常在分支方法内归一化为 ``status="error"``。

        副作用:
            见各分支方法 docstring；本方法仅做分流，不直接启动进程或线程。
        """

        if tool.execution_mode == "process":
            return self._execute_in_process(
                tool,
                arguments,
                execution_context,
                tool_call_id,
                should_cancel=should_cancel,
                output_sink=output_sink,
            )
        return self._execute_in_thread(tool, arguments, execution_context, tool_call_id)

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
        output_sink: OutputSink | None = None,
    ) -> ToolObservation:
        """在隔离子进程中执行单个工具 handler 并返回归一化结果。

        参数:
            tool: 工具定义，提供 handler、权限、超时等执行契约。
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；
                跨进程序列化后由 handler 在执行期消费，便于后续扩展执行参数。
            tool_call_id: 关联本次执行的模型工具调用 id，用于回写观察结果。
            should_cancel: 可选取消检查回调；返回 True 时终止子进程并返回取消观察。
            output_sink: 可选实时输出回调；非 None 时额外建立跨进程输出队列，
                父进程在等待结果的轮询间隙 drain 队列并回调它。

        返回:
            归一化后的 :class:`ToolObservation`：成功为 status="success"，
            失败/超时/异常/启动失败为 status="error"（含 reason 与 retryable）。

        异常:
            不向上抛出：``timeout_seconds`` 为 None / <=0 时直接返回 ``status="error"``
            的 :class:`ToolObservation`（``reason`` 为配置缺失的富文本提示），**不抛**
            ``ValueError``；其余执行异常亦在分支方法内归一化为 ``status="error"``
            （``reason`` 为面向模型的富文本，而非稳定机器短码）。
            调用方若需兜底，应判断返回的 :class:`ToolObservation` 状态，而非捕获异常。

        副作用:
            启动一个守护子进程执行 handler；按 ``timeout_seconds`` 软超时后
            强制 terminate/kill 并清理进程；以 INFO/WARNING/ERROR 级别写入
            执行、超时、失败日志；不修改 ``tool`` 或 ``arguments``。
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

        # --- 2. 启动隔离子进程 ---
        result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1)
        # 放弃等待 feeder 线程 flush：子进程已死或已 drain 后不阻塞父进程退出
        result_queue.cancel_join_thread()
        # 取父进程已建好的跨进程日志队列；为 None 表示父进程未启用日志桥，
        # 子进程退化为默认 logging（不接入统一管线），不影响工具执行本身。
        log_queue = get_log_queue()
        output_queue: multiprocessing.Queue | None = None
        if output_sink is not None:
            # 有界队列：消费端（父进程轮询）跟不上时子进程侧丢片段而非反压命令执行。
            output_queue = multiprocessing.Queue(maxsize=_OUTPUT_QUEUE_MAXSIZE)
            output_queue.cancel_join_thread()
        process_execution_context = (
            execution_context.for_process_execution() if execution_context is not None else None
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
            log.info(
                "tool_execution_cancelled",
                extra={
                    "msg": "工具执行因 turn 取消而中止",
                    "data": {
                        "error_kind": ErrorKind.RUNTIME_FAILED.value,
                        "tool_name": tool.name,
                    },
                },
            )
            return tool_error(
                tool.name,
                "tool execution cancelled",
                reason=(
                    "the current turn was cancelled while this tool was running; "
                    "the tool process was terminated and no further action is needed."
                ),
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
                error_kind=ErrorKind.RUNTIME_FAILED,
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
                error_kind=ErrorKind.RUNTIME_FAILED,
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
                error_kind=ErrorKind.RUNTIME_FAILED,
            )
        finally:
            if process.is_alive():
                self._force_kill(process)
                process.join(2)

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
                error_kind=ErrorKind.RUNTIME_FAILED,
            )

        return self._normalize_result(tool, payload, tool_call_id)

    @staticmethod
    def _drain_output_queue(
        output_queue: "multiprocessing.Queue | None",
        output_sink: OutputSink | None,
    ) -> None:
        """把输出队列中当前已积压的片段全部取出并回调 ``output_sink``。

        非阻塞：只 drain「此刻已就绪」的片段，读空即返回，因此可安全地放在
        等待结果的轮询间隙反复调用，实现运行期实时推送。

        参数:
            output_queue: 跨进程输出队列；为 None 时直接返回。
            output_sink: 片段消费回调；为 None 时直接返回。

        返回:
            无。

        异常:
            不向上抛出：队列已关闭 / 管道损坏 / sink 自身抛错，均视为实时通道
            失效并静默结束本次 drain，不影响工具结果的获取与归一化。

        副作用:
            消费队列中的片段并逐条调用 ``output_sink``。
        """
        if output_queue is None or output_sink is None:
            return
        while True:
            try:
                text, truncated = output_queue.get_nowait()
            except queue.Empty:
                return
            except Exception:
                # 队列已关闭或管道损坏：实时通道到此为止，最终输出仍由 result_queue
                # 保证。留 debug 痕迹便于事后排查实时通道失效原因，不反压命令执行。
                log.debug(
                    "tool_output_queue_drain_aborted",
                    extra={"msg": "实时输出队列读取异常，结束本轮 drain", "data": {}},
                )
                return
            try:
                output_sink(text, truncated)
            except Exception:
                # sink 自身故障（如前端断连）不影响工具结果获取，留痕后停止推送。
                log.debug(
                    "tool_output_sink_callback_failed",
                    extra={"msg": "实时输出回调异常，停止推送", "data": {}},
                )
                return

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
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                # 取消终态：先收尾已产生的输出，再抛出取消信号。
                ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                raise _ToolExecutionCancelled()
            remaining = deadline - time.monotonic()
            try:
                result = result_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                # 空转即实时 drain：这是运行期增量能实时到达前端的关键落点，
                # 缺少此处会退化为命令结束后一次性刷出。
                ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                if process.is_alive():
                    continue
                # 进程已退出：再给 0.1s 做最后一次非阻塞读取，仍无结果则判定无结果。
                try:
                    result = result_queue.get(timeout=0.1)
                except queue.Empty:
                    ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                    break
                ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
                return result
            # 成功拿到结果：先 drain 尾部片段再返回，避免最后几行输出丢失。
            ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
            return result
        if process.is_alive():
            # 超时终态：强杀前收尾，保留已产生的部分输出。
            ToolHandlerRunner._drain_output_queue(output_queue, output_sink)
            raise TimeoutError()
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
    ) -> ToolObservation:
        """在当前调用线程直接执行 handler 并归一化结果（无子进程隔离）。

        参数:
            tool: 工具定义，提供 handler、权限等执行契约。
            arguments: 已通过准入门禁与参数校验的关键字参数字典。
            execution_context: 本次执行的运行时边界，直接作为关键字参数注入 handler
                （同进程，无需 pickle 序列化）。
            tool_call_id: 关联本次执行的模型工具调用 id。

        返回:
            归一化后的 :class:`ToolObservation`。

        异常:
            不向上抛出：handler 抛出的任意异常被捕获并归一化为
            ``status="error"``（``reason`` 为面向模型的富文本，而非稳定机器短码）。

        副作用:
            在调用方线程内同步执行 handler；直接用主进程 ``log`` 单例；**不启动
            子进程、不建 Queue、不挂载日志桥、不做硬超时强杀**。`execution_mode
            != "process"` 时 ``tool.timeout_seconds`` 仅作元数据，本方法不据此
            监控 / 强杀线程。
        """
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
                error_kind=ErrorKind.RUNTIME_FAILED,
            )
        return self._normalize_result(tool, result, tool_call_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
                作为关键字参数 ``execution_context`` 注入 handler。
            output_queue: 可选实时输出队列；非 None 时以关键字参数 ``output_sink``
                注入 handler，handler 可在运行期回传 ``(text, truncated)`` 片段。
                队列满时片段被丢弃而非阻塞，保证命令执行不被消费端拖慢。

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
            # 导向父进程队列，复用父进程已配好的脱敏/截断/上下文关联。
            from app.config.logging.configuration import (
                install_logging_for_current_process,
            )

            install_logging_for_current_process(log_queue=log_queue)

        extra_kwargs: dict[str, Any] = {}
        if output_queue is not None:

            def _output_sink(text: str, truncated: bool) -> None:
                """把一段输出片段非阻塞地回传父进程。

                参数:
                    text: 已脱敏的增量输出片段。
                    truncated: 实时通道预算是否已耗尽。

                返回:
                    无。

                异常:
                    不向上抛出：队列已满或已关闭时静默丢弃该片段，实时展示属旁路
                    能力，不得反压命令执行或使 handler 失败。

                副作用:
                    向跨进程 ``output_queue`` 写入一条 ``(text, truncated)`` 元组；
                    队列满或管道损坏时丢弃该片段并留 debug 痕迹。
                """
                # 队列满 / 已关闭时丢弃该片段：实时展示是旁路能力，绝不能反压
                # 命令执行，最终完整输出仍由 result_queue 保证。丢弃留 debug
                # 痕迹（debug 级生产默认关闭，避免高频片段刷日志）。
                try:
                    output_queue.put_nowait((text, truncated))
                except Exception as exc:
                    log.debug(
                        "tool_output_fragment_dropped",
                        extra={
                            "msg": "实时输出片段入队失败已丢弃",
                            "data": {"error": str(exc), "text_len": len(text)},
                        },
                    )

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
                # 与 result_queue 同理：feeder 线程需 flush 完剩余片段再退出，
                # 否则末尾输出会随进程退出丢失。flush 失败只丢尾部展示数据
                # （结果已入 result_queue），留 debug 痕迹便于排查尾部缺行问题。
                try:
                    output_queue.close()
                    output_queue.join_thread()
                except Exception as exc:
                    log.debug(
                        "tool_output_queue_flush_failed",
                        extra={
                            "msg": "子进程退出前实时输出队列 flush 失败",
                            "data": {"error": str(exc)},
                        },
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
