"""Process-local cancellation signals for individually running tool calls."""

from __future__ import annotations

import threading

from app.config.logging.logger import log


class ToolCallCancellationRegistry:
    """Track per-tool-call cancellation requests for the current process.

    与 run 级取消注册表（``conversation_run_cancellation_registry``）职责不同：run 级
    取消要求整个 Conversation Run 停止执行，而本注册表只要求**一次工具调用**停止执行，
    run 与 Agent 在其后继续运行。键是 ``(run_id, tool_call_id)`` 二元组，因此同一批
    并行工具调用中只中止被点名的那一个。

    本类只表达运行时控制信号，不是持久化事实来源。工具是否真的被中断、以及工具调用的
    终态，仍由工具执行层产出的 ``ToolObservation`` 与 Conversation Run 事实承载。

    键的规范化契约：注册表键恒为 ``(int, str)``。写入与查询都会把 run 标识归一为整数
    （``"7"`` 与 ``7`` 是同一个键）；无法表示为整数的 run 标识构不成合法键——读取一律
    返回未取消，写入与清理只记 warning 后跳过。这里刻意不抛异常：本注册表被工具执行的
    50ms 轮询与 ``finally`` 收尾路径调用，异常会污染工具结果，且 run 标识的合法性由
    调用方（HTTP 路径参数 ``int`` / ``ToolExecutionContext.run_id``）保证。
    """

    def __init__(self) -> None:
        """Initialize an empty in-memory per-tool-call cancellation registry.

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            创建线程锁与内存集合。
        """

        self._lock = threading.Lock()
        self._cancelled_calls: set[tuple[int, str]] = set()

    def mark_cancelled(self, run_id: int | str, tool_call_id: str) -> None:
        """Mark one tool call of one Conversation Run as cancelled in this process.

        参数:
            run_id: 目标工具调用所属的 Conversation Run 标识；数字字符串会被归一为整数。
            tool_call_id: 目标工具调用标识（模型工具调用 id）。

        返回:
            无。

        异常:
            无。

        副作用:
            写入进程内取消集合，并记录一条 INFO 日志；重复标记幂等。run 标识无法归一为
            整数时只记 warning 并跳过写入（见类 docstring 的键规范化契约）。
        """

        key = self._key(run_id, tool_call_id, operation="mark_cancelled")
        if key is None:
            return
        with self._lock:
            self._cancelled_calls.add(key)
        log.info(
            "tool_call_cancellation_marked",
            extra={
                "msg": "tool call cancellation signal marked",
                "data": {"run_id": key[0], "tool_call_id": key[1]},
            },
        )

    def is_cancelled(self, run_id: int | str, tool_call_id: str) -> bool:
        """Return whether one tool call has a process-local cancellation signal.

        参数:
            run_id: 目标工具调用所属的 Conversation Run 标识；数字字符串会被归一为整数。
            tool_call_id: 目标工具调用标识；为空字符串时一律返回 False（「没有工具调用
                身份」不构成可取消范围）。

        返回:
            该 ``(run_id, tool_call_id)`` 已标记取消时为 True，否则为 False。run 标识不
            可归一或工具调用标识为空时返回 False。

        异常:
            无。

        副作用:
            无。
        """

        key = self._key(run_id, tool_call_id, operation=None)
        if key is None:
            return False
        with self._lock:
            return key in self._cancelled_calls

    def clear(self, run_id: int | str, tool_call_id: str) -> None:
        """Remove one tool call cancellation signal.

        工具执行层在本次调用结束时调用本方法，使信号生命周期与调用生命周期一致；
        是否真的存在信号由调用方决定，重复清理是安全的。

        参数:
            run_id: 目标工具调用所属的 Conversation Run 标识。
            tool_call_id: 目标工具调用标识。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内取消集合移除对应标记；run 标识不可归一时只记 warning 并跳过。
        """

        key = self._key(run_id, tool_call_id, operation="clear")
        if key is None:
            return
        with self._lock:
            self._cancelled_calls.discard(key)

    def clear_run(self, run_id: int | str) -> None:
        """Remove every tool call cancellation signal of one Conversation Run.

        兜底清理入口：run 执行收尾时调用，用于回收「已过期的工具调用被点名取消」这类
        不会再被工具执行层消费、也不会自行释放的信号。

        参数:
            run_id: 待清理的 Conversation Run 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            从进程内取消集合移除该 run 的全部标记；实际清理条数非零时写一条含条数的
            INFO 日志（不写工具调用 id 列表，避免无边界日志体量）。run 标识不可归一时
            只记 warning 并跳过。
        """

        run_key = self._int_run_id(run_id)
        if run_key is None:
            log.warning(
                "tool_call_cancellation_invalid_run_id",
                extra={
                    "msg": "工具级取消信号的 run 标识无法归一为整数，跳过清理",
                    "data": {"operation": "clear_run", "run_id": repr(run_id)},
                },
            )
            return
        with self._lock:
            removed = {key for key in self._cancelled_calls if key[0] == run_key}
            self._cancelled_calls -= removed
        if removed:
            log.info(
                "tool_call_cancellation_run_cleared",
                extra={
                    "msg": "run 收尾时清理了遗留的工具级取消信号",
                    "data": {"run_id": run_key, "cleared_count": len(removed)},
                },
            )

    @classmethod
    def _key(
        cls,
        run_id: int | str,
        tool_call_id: str,
        *,
        operation: str | None,
    ) -> tuple[int, str] | None:
        """把一次工具级取消读写归一为注册表键。

        参数:
            run_id: 原始 run 标识。
            tool_call_id: 原始工具调用标识；空值不构成合法键。
            operation: 调用方操作名，仅用于 run 标识非法时的 warning 日志；为 None 表示
                读取路径（读取失败是正常结果，不记日志）。

        返回:
            ``(run_id, tool_call_id)`` 规范键；``tool_call_id`` 为空或 ``run_id`` 无法
            归一为整数时返回 None，调用方按「不可命中 / 不可写入」处理。

        异常:
            无。

        副作用:
            ``operation`` 非 None 且 run 标识非法时写一条 warning 日志。
        """

        if not tool_call_id:
            return None
        run_key = cls._int_run_id(run_id)
        if run_key is None:
            if operation is not None:
                log.warning(
                    "tool_call_cancellation_invalid_run_id",
                    extra={
                        "msg": "工具级取消信号的 run 标识无法归一为整数，跳过该操作",
                        "data": {"operation": operation, "run_id": repr(run_id)},
                    },
                )
            return None
        return run_key, tool_call_id

    @staticmethod
    def _int_run_id(run_id: int | str) -> int | None:
        """把 run 标识归一为整数；无法表示时返回 None。"""

        if isinstance(run_id, int):
            return run_id
        try:
            return int(run_id)
        except (TypeError, ValueError):
            return None


tool_call_cancellation_registry = ToolCallCancellationRegistry()
