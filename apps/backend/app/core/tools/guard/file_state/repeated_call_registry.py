"""按 task 维护有界的只读工具重复调用状态。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.core.tools.schemas.tool_names import TOOL_READ_FILE


@dataclass(frozen=True)
class RepeatedCallAction:
    """重复调用判定结果。"""

    action: str
    repeat_count: int


@dataclass
class _RepeatedCallState:
    fingerprint: Any
    repeat_count: int


class RepeatedCallRegistry:
    """维护 task 隔离、LRU 有界的 read/search 重复调用计数。"""

    def __init__(self, *, max_tasks: int = 128, max_calls_per_task: int = 512) -> None:
        """初始化重复调用 registry。

        参数:
            max_tasks: 最多保留的 task 数。
            max_calls_per_task: 每个 task 最多保留的调用签名数。

        返回:
            无。

        异常:
            ValueError: 任一容量小于 1 时抛出。

        副作用:
            创建进程内锁与空的 LRU 状态。
        """

        if max_tasks < 1 or max_calls_per_task < 1:
            raise ValueError("repeated call registry capacities must be greater than zero")
        self._max_tasks = max_tasks
        self._max_calls_per_task = max_calls_per_task
        self._tasks: OrderedDict[str, OrderedDict[str, _RepeatedCallState]] = OrderedDict()
        self._lock = threading.RLock()

    def check(
        self,
        task_id: str,
        signature: str,
        fingerprint: Any,
        *,
        tool_name: str,
    ) -> RepeatedCallAction:
        """判定本次调用应执行、提示 unchanged，还是硬阻断。

        参数:
            task_id: 状态所属任务。
            signature: 归一化调用签名。
            fingerprint: 当前文件或搜索范围快照。
            tool_name: 当前工具名，仅支持 read_file / search_content / find_files 特定策略。

        返回:
            ``execute`` / ``unchanged`` / ``warning`` / ``block`` 动作。

        异常:
            无。

        副作用:
            仅已有成功基线时更新重复计数与 LRU；需要执行时不提前登记。
        """

        with self._lock:
            task_calls = self._task_calls(task_id)
            previous = task_calls.get(signature)
            if previous is None or previous.fingerprint != fingerprint:
                return RepeatedCallAction("execute", 0)

            previous.repeat_count += 1
            task_calls.move_to_end(signature)
            if tool_name == TOOL_READ_FILE:
                return RepeatedCallAction("unchanged", previous.repeat_count)
            if previous.repeat_count == 1:
                return RepeatedCallAction("warning", previous.repeat_count)
            return RepeatedCallAction("block", previous.repeat_count)

    def record_success(self, task_id: str, signature: str, fingerprint: Any) -> None:
        """在一次真实调用成功后登记可复用结果的状态基线。

        参数:
            task_id: 状态所属任务。
            signature: 归一化调用签名。
            fingerprint: 调用开始前观察到的文件或搜索范围快照。

        返回:
            无。

        异常:
            无。

        副作用:
            写入或刷新成功基线；并发成功调用不会累计为重复跳过次数。
        """

        with self._lock:
            task_calls = self._task_calls(task_id)
            previous = task_calls.get(signature)
            if previous is None or previous.fingerprint != fingerprint:
                task_calls[signature] = _RepeatedCallState(
                    fingerprint=fingerprint,
                    repeat_count=0,
                )
            else:
                previous.repeat_count = 0
                task_calls.move_to_end(signature)
            self._trim(task_calls)

    def clear_task(self, task_id: str) -> None:
        """清除一个 task 的重复调用状态。

        参数:
            task_id: 待清除任务。

        返回:
            无。

        异常:
            无。

        副作用:
            删除进程内 task 状态。
        """

        with self._lock:
            self._tasks.pop(task_id, None)

    def _task_calls(self, task_id: str) -> OrderedDict[str, _RepeatedCallState]:
        """取得或创建 task 调用状态。

        参数:
            task_id: 状态所属任务。

        返回:
            task 对应的有序调用字典。

        异常:
            无。

        副作用:
            可能创建 task 状态并淘汰最旧 task。
        """

        task_calls = self._tasks.get(task_id)
        if task_calls is None:
            task_calls = OrderedDict()
            self._tasks[task_id] = task_calls
        self._tasks.move_to_end(task_id)
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)
        return task_calls

    def _trim(self, task_calls: OrderedDict[str, _RepeatedCallState]) -> None:
        """按单 task 容量淘汰最旧调用签名。

        参数:
            task_calls: 待裁剪的 task 调用字典。

        返回:
            无。

        异常:
            无。

        副作用:
            可能删除最旧调用状态。
        """

        while len(task_calls) > self._max_calls_per_task:
            task_calls.popitem(last=False)
