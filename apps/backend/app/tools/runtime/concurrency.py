"""工具调用并发调度器。"""

from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Callable, Sequence

from app.tools.execution.concurrency import ToolConcurrencyPlan
from app.tools.executor import PreparedToolCall
from app.tools.runtime.locks import ToolResourceLockManager
from app.tools.results import ToolRuntimeResult


class ToolConcurrentScheduler:
    """按并发计划执行已准备的工具调用。"""

    def __init__(self, locks: ToolResourceLockManager, logger: logging.Logger, max_workers: int = 4) -> None:
        """初始化并发调度器。

        参数:
            locks: 运行期资源锁管理器。
            logger: 记录组级别诊断的日志器。
            max_workers: 同时执行 handler 的最大线程数。

        返回:
            无。

        异常:
            ValueError: 当最大并发数小于 1 时抛出。

        副作用:
            保存调度依赖。
        """

        if max_workers < 1:
            raise ValueError("max_workers must be greater than zero")
        self._locks = locks
        self._logger = logger
        self._max_workers = max_workers

    def run(
        self,
        plan: ToolConcurrencyPlan,
        prepared_groups: Sequence[Sequence[PreparedToolCall]],
        single_call_executor: Callable[[PreparedToolCall], ToolRuntimeResult],
    ) -> list[ToolRuntimeResult]:
        """按计划执行调用并保持输入顺序返回结果。

        参数:
            plan: 由 ToolConcurrencyPlanner 构造的分组计划。
            prepared_groups: 与计划分组一一对应的已准备调用。
            single_call_executor: 不反向依赖 Runtime 的单调用执行回调。

        返回:
            与输入调用顺序对齐的运行时结果列表。

        异常:
            ValueError: 当计划与已准备分组数量不一致时抛出。

        副作用:
            执行工具 handler，并为资源冲突调用获取锁。
        """

        if len(plan.groups) != len(prepared_groups):
            raise ValueError("plan and prepared groups must have equal length")
        results: list[ToolRuntimeResult] = []
        for group, prepared_calls in zip(plan.groups, prepared_groups):
            self._logger.info(
                "tool_concurrency_group_started",
                extra={
                    "parallel": group.parallel,
                    "reason": group.reason,
                    "size": len(prepared_calls),
                },
            )
            if group.parallel and len(prepared_calls) > 1:
                with ThreadPoolExecutor(max_workers=min(self._max_workers, len(prepared_calls))) as executor:
                    futures = [executor.submit(self._execute_with_lock, call, single_call_executor) for call in prepared_calls]
                    results.extend(future.result() for future in futures)
            else:
                results.extend(self._execute_with_lock(call, single_call_executor) for call in prepared_calls)
            self._logger.info(
                "tool_concurrency_group_finished",
                extra={
                    "parallel": group.parallel,
                    "reason": group.reason,
                    "size": len(prepared_calls),
                },
            )
        return results

    def _execute_with_lock(
        self,
        prepared: PreparedToolCall,
        single_call_executor: Callable[[PreparedToolCall], ToolRuntimeResult],
    ) -> ToolRuntimeResult:
        """在声明资源锁保护下执行单个调用。

        参数:
            prepared: 已准备调用。
            single_call_executor: 窄执行回调。

        返回:
            单调用运行时结果。

        异常:
            Exception: 透传无法归一化的基础设施异常。

        副作用:
            获取资源锁并调用 handler 执行器。
        """

        with self._locks.acquire(prepared.tool.resource_keys):
            return single_call_executor(prepared)
