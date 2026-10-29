"""延迟工具结果登记表：一次工具调用登记一条、结论稍后以 ``background_result`` 回投的工作。

设计见 ``docs/plan/deferred-tool-result-design.md``。条目以 ``tool_call_id`` 为键，每条恰好产出一个
结论；登记表只记录「结论回投给谁、谁在等、撤销时通知谁」，不持有工作本身的执行状态——工作的
生命周期归各执行单元（终端 runtime 等），登记表不是任何一种 Run 的第二套状态机。

为什么放在 core：``wait_messages`` 判定是否还有可等待的工作、``TaskActor`` 在 Run 停止时撤销条目，
都在 core；而具体的工作在 ``app.tools`` 一侧实现。依赖方向只允许 ``app.tools → core``，所以由
执行单元显式 ``register`` 并传入撤销回调（依赖倒置），core 不 import 工具层。

事实边界：进程内协调状态，不持久化、不跨重启、重启后不重建；重启收尾见 ``restart_settlement``。

两种监听（``mode``）：后台条目的结论以消息回投；前台条目的工具线程还在等，结论作为工具结果返回，
可被 ``detach`` 改为后台（转到后台）。只有后台条目在对话里有占位行（卡片状态见设计文档「卡片
状态」），占位行的结算由调用方完成，登记表不碰 context。

生命周期（每条恰好结束一次，由 ``_entries`` 的 pop 保证）：
- 完成：执行单元产出结论时调用 ``complete``，出队与投递在同一入口（pop-once 防重复投递）；Task
  正在停止时 ``complete`` 不出队，条目由随后的撤销结束（结局只有「投递」与「撤销」两种）；前台
  条目正常结束时调用 ``settle_foreground``，与 ``detach`` 在同一把锁内裁决先后；
- 撤销：Run 停止（用户停止、父 Task 停止）时 ``TaskCancellation`` 调用 ``drop_task``，Task 删除
  提交后 ``TaskService`` 同样调用，撤销该 Task 的全部条目（含更早 Run 登记的）并逐条调用撤销回调，
  之后到达的 ``complete`` 不投递；用户在看板取消单条、编辑重跑撤回所在一轮的调用走 ``cancel``；
- 关闭：后端关闭时 lifespan 在停 actor、关终端之前调用 ``close``：撤销全部条目并等在途投递落定，
  此后 ``complete`` 一律不投递。关闭期间仍在跑的后台命令被杀后会以结论回调，若不先关闭，
  这些线程会在服务依赖关闭期间写 ``task_messages``，重启后挂起的 Run 先收到过期结论再收到
  ``backend_restarted``。投递只能经 ``complete``，``close`` 才能保证「关闭期间不写库」。

看板：``entries`` 只给后台工作——后台活条目与每 Task 最近结束的若干条（进程内，不跨重启）。前台条目
不进看板，结束后也不留记录：结论已作为工具结果返回，它只是一次普通的工具调用；若让每次前台等待
（``terminal_write`` 每次都等）都进看板，「最近结束」会被它们挤满。前台条目只经 ``foreground_calls``
告诉工具卡片「可以转到后台」，转到后台后才算后台工作。任何变化都经单线程通知执行器在锁外回调
``set_listener`` 注入的监听方（lifespan 注入 snapshot 投影，core 不依赖 transport）。不能在调用方
线程同步回调：登记常发生在终端会话锁内，而投影可能反过来读终端会话。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.config.logging.logger import log
from app.core.tool.background.deferred_job import DeferredConclusion
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.message_queue.task_message import BackgroundResultBody, TaskMessage

# 条目种类，同时作为 ``background_result`` 的 ``tool_name``：给模型标明是哪类后台工作，不一定是
# 可调用的工具名。
DeferredKind = Literal["terminal_command", "child_agent"]
DeferredMode = Literal["foreground", "background"]
DeferredStatus = Literal["running", "completed", "cancelled"]
ForegroundOutcome = Literal["settled", "detached", "gone"]
DetachOutcome = Literal["detached", "not_found", "not_foreground"]

# 每个 Task 保留的最近结束条目数，供看板「已完成」分组；与终端已结束会话的保留数同一量级。
MAX_ENDED_PER_TASK = 20


@dataclass
class _Entry:
    task_id: int
    run_id: int | None
    kind: DeferredKind
    label: str
    mode: DeferredMode
    started_at: str
    on_cancel: Callable[[], None]
    on_detach: Callable[[], None] | None


@dataclass(frozen=True)
class DeferredEntry:
    """看板读取用的后台条目快照；``ended_at`` 为 ``None`` 表示仍在运行。"""

    tool_call_id: str
    kind: DeferredKind
    label: str
    status: DeferredStatus
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class DroppedEntry:
    """被撤销的条目：调用方据 ``mode`` 判断有没有占位行要结算。"""

    tool_call_id: str
    task_id: int
    mode: DeferredMode


class DeferredRegistry:
    """按 ``tool_call_id`` 登记未完成的延迟结果；线程安全，任意线程可调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._entries: dict[str, _Entry] = {}
        self._ended: dict[int, deque[DeferredEntry]] = {}
        self._closed = False
        self._delivering = 0
        self._listener: Callable[[int], None] | None = None
        self._notifier = ThreadPoolExecutor(max_workers=1, thread_name_prefix="background-notify")

    def open(self) -> None:
        """lifespan 启动时开放投递；同一进程内 lifespan 可能多次运行（测试），关闭状态不能残留。"""

        with self._lock:
            self._closed = False

    def set_listener(self, listener: Callable[[int], None] | None) -> None:
        """注入变化监听方（按 task_id 回调，在通知线程、锁外执行）；``None`` 解除。"""

        with self._lock:
            self._listener = listener

    def register(
        self,
        tool_call_id: str,
        *,
        task_id: int,
        run_id: int | None,
        kind: DeferredKind,
        label: str,
        on_cancel: Callable[[], None],
        mode: DeferredMode = "background",
        on_detach: Callable[[], None] | None = None,
    ) -> None:
        """登记一条延迟结果；``on_cancel`` 在撤销时调用（锁外、只调一次）。

        前台条目须给 ``on_detach``：转到后台时调用，唤醒还在等的工具线程。``tool_call_id`` 由调用方
        保证本进程内唯一（模型给出的单次调用身份）；重复登记是调用方缺陷，抛 ``ValueError``。后端
        关闭后的登记不入表：它的 ``complete`` 自然不投递，执行单元随会话结束收尾。
        """

        data = {
            "task_id": task_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "kind": kind,
            "mode": mode,
        }
        with self._lock:
            if tool_call_id in self._entries:
                raise ValueError(f"background result already registered: {tool_call_id}")
            closed = self._closed
            if not closed:
                self._entries[tool_call_id] = _Entry(
                    task_id, run_id, kind, label, mode, _now(), on_cancel, on_detach
                )
                pending = sum(entry.task_id == task_id for entry in self._entries.values())
        if closed:
            log.info(
                "deferred_result_rejected_closed",
                extra={"msg": "后端正在关闭，延迟结果不登记", "data": data},
            )
            return
        log.info(
            "deferred_result_registered",
            extra={"msg": "登记延迟结果", "data": {**data, "label": label, "pending": pending}},
        )
        self._notify(task_id)

    def complete(self, tool_call_id: str, conclusion: DeferredConclusion) -> bool:
        """结束一条登记并把结论投递给它的 Task；已撤销、已完成、已关闭或 Task 正在停止时不投递，
        返回 False。

        出队与「在途计数」在同一把锁内：``close`` 要么先于出队（不投递），要么等这次投递落定。
        Task 正在停止（有取消信号）时不出队：停止期间到达的结论已无人需要，条目留给执行收尾的
        ``drop_task`` 统一撤销，占位行随撤销推到 ``cancelled``。若在这里出队再丢弃，撤销看不到它，
        结论也不会并入，占位行就没有任何结算路径。取消信号只在有执行者时打出，且执行收尾先撤销
        再取走信号，所以留下的条目必然被撤销。
        会同步写 SQLite，调用方不得在 PTY 读线程等热路径上调用。
        """

        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None or self._closed:
                return False
            if cancellation_registry.is_cancelled(entry.task_id):
                log.info(
                    "deferred_result_left_for_drop",
                    extra={
                        "msg": "Task 正在停止，结论不投递，条目留给撤销",
                        "data": {"task_id": entry.task_id, "tool_call_id": tool_call_id},
                    },
                )
                return False
            del self._entries[tool_call_id]
            self._end(tool_call_id, entry, "completed")
            self._delivering += 1
        try:
            return deliver_background_result(
                entry.task_id,
                tool_call_id=tool_call_id,
                tool_name=entry.kind,
                result=conclusion.result,
                placeholder_display_data=conclusion.display_data,
            )
        finally:
            with self._lock:
                self._delivering -= 1
                self._settled.notify_all()
            self._notify(entry.task_id)

    def settle_foreground(self, tool_call_id: str) -> ForegroundOutcome:
        """前台等待正常结束：仍是前台则出队（结论作为工具结果返回，不投递），返回 ``settled``。

        与 :meth:`detach` 在同一把锁内裁决：``detached`` 表示转后台先到，调用方须改用
        :meth:`complete` 回投已拿到的结论并返回「已转后台」占位；``gone`` 表示条目已被撤销或关闭，
        调用方按自己的取消结果返回。
        """

        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None:
                return "gone"
            if entry.mode == "background":
                return "detached"
            del self._entries[tool_call_id]
        self._notify(entry.task_id)
        return "settled"

    def detach(self, tool_call_id: str) -> DetachOutcome:
        """把前台条目改为后台并调用 ``on_detach`` 唤醒等待线程（锁外）。"""

        with self._lock:
            entry = self._entries.get(tool_call_id)
            if entry is None:
                return "not_found"
            if entry.mode != "foreground" or entry.on_detach is None:
                return "not_foreground"
            entry.mode = "background"
            on_detach = entry.on_detach
        log.info(
            "deferred_result_detached",
            extra={
                "msg": "前台等待转到后台",
                "data": {"task_id": entry.task_id, "tool_call_id": tool_call_id},
            },
        )
        on_detach()
        self._notify(entry.task_id)
        return "detached"

    def cancel(self, tool_call_id: str, *, reason: str = "用户取消，撤销延迟结果") -> DroppedEntry | None:
        """撤销单条（用户取消、编辑重跑撤回所在一轮）：出队并调用撤销回调；没有该条目时返回 ``None``。

        ``reason`` 只进日志，区分撤销来源。
        """

        with self._lock:
            entry = self._entries.pop(tool_call_id, None)
            if entry is None:
                return None
            self._end(tool_call_id, entry, "cancelled")
        self._cancel_all([(tool_call_id, entry)], reason)
        self._notify(entry.task_id)
        return DroppedEntry(tool_call_id, entry.task_id, entry.mode)

    def discard(self, tool_call_id: str) -> None:
        """移除一条登记且不调撤销回调：登记方在工作真正启动前失败（如投递入队失败）时收回。"""

        with self._lock:
            entry = self._entries.pop(tool_call_id, None)
        if entry is not None:
            log.info(
                "deferred_result_discarded",
                extra={
                    "msg": "工作未启动，收回延迟结果登记",
                    "data": {"task_id": entry.task_id, "tool_call_id": tool_call_id},
                },
            )
            self._notify(entry.task_id)

    def pending(self, task_id: int) -> list[str]:
        """返回 Task 下未完成条目的标签，供 ``wait_messages`` 判定与提示。"""

        with self._lock:
            return [entry.label for entry in self._entries.values() if entry.task_id == task_id]

    def owns(self, task_id: int, tool_call_id: str) -> bool:
        """这次调用是否有属于该 Task 的活条目（前台或后台；接口层判定寻址是否属于该 Task）。"""

        with self._lock:
            entry = self._entries.get(tool_call_id)
            return entry is not None and entry.task_id == task_id

    def entries(self, task_id: int) -> list[DeferredEntry]:
        """看板条目：后台活条目按登记先后在前，之后是最近结束的（新的在后）；前台条目不在其中。"""

        with self._lock:
            live = [
                _view(tool_call_id, entry, "running", None)
                for tool_call_id, entry in self._entries.items()
                if entry.task_id == task_id and entry.mode == "background"
            ]
            return [*live, *self._ended.get(task_id, ())]

    def foreground_calls(self, task_id: int) -> list[str]:
        """Task 下正在前台等待、可以转到后台的调用（按登记先后），供工具卡片的「转到后台」。"""

        with self._lock:
            return [
                tool_call_id
                for tool_call_id, entry in self._entries.items()
                if entry.task_id == task_id
                and entry.mode == "foreground"
                and entry.on_detach is not None
            ]

    def drop_task(self, task_id: int) -> list[DroppedEntry]:
        """撤销 Task 的全部条目（Run 停止、Task 删除提交后调用）；回调异常记录后继续，不阻断收尾。

        返回被撤销的条目，Run 停止的调用方据此结算后台占位行（删除时占位行已不在）。
        """

        with self._lock:
            dropped = [
                (tool_call_id, entry)
                for tool_call_id, entry in self._entries.items()
                if entry.task_id == task_id
            ]
            for tool_call_id, entry in dropped:
                del self._entries[tool_call_id]
                self._end(tool_call_id, entry, "cancelled")
        self._cancel_all(dropped, "Run 停止，撤销延迟结果")
        if dropped:
            self._notify(task_id)
        return [
            DroppedEntry(tool_call_id, entry.task_id, entry.mode) for tool_call_id, entry in dropped
        ]

    def forget_task(self, task_id: int) -> None:
        """Task 删除后清掉它的最近结束条目；活条目须先经 :meth:`drop_task` 撤销。"""

        with self._lock:
            self._ended.pop(task_id, None)

    def close(self) -> None:
        """后端关闭：此后不再投递任何结论，撤销全部条目并等在途投递写完。

        先于 actor 与终端关闭调用（见模块说明）；会阻塞到在途投递落定，须在事件循环外调用。
        """

        with self._lock:
            self._closed = True
            dropped = list(self._entries.items())
            self._entries.clear()
            self._ended.clear()
            while self._delivering:
                self._settled.wait()
        self._cancel_all(dropped, "后端关闭，撤销延迟结果")

    def _end(self, tool_call_id: str, entry: _Entry, status: DeferredStatus) -> None:
        """持锁调用：把结束的后台条目记进该 Task 的最近结束列表；前台条目不留记录（见模块说明）。"""

        if entry.mode != "background":
            return
        ended = self._ended.setdefault(entry.task_id, deque(maxlen=MAX_ENDED_PER_TASK))
        ended.append(_view(tool_call_id, entry, status, _now()))

    def _notify(self, task_id: int) -> None:
        """锁外把变化交给通知线程；监听方异常记录后丢弃，看板是诊断旁路、不影响结论投递。"""

        listener = self._listener
        if listener is None:
            return

        def run() -> None:
            try:
                listener(task_id)
            except Exception:
                log.exception(
                    "deferred_result_listener_failed",
                    extra={"msg": "延迟结果变化通知失败", "data": {"task_id": task_id}},
                )

        self._notifier.submit(run)

    @staticmethod
    def _cancel_all(dropped: list[tuple[str, _Entry]], msg: str) -> None:
        """锁外逐条调用撤销回调；回调异常记录后继续，不阻断取消或关闭收尾。"""

        for tool_call_id, entry in dropped:
            data = {"task_id": entry.task_id, "tool_call_id": tool_call_id, "kind": entry.kind}
            try:
                entry.on_cancel()
            except Exception:
                log.exception(
                    "deferred_result_cancel_failed",
                    extra={"msg": "撤销延迟结果的回调失败", "data": data},
                )
            log.info("deferred_result_dropped", extra={"msg": msg, "data": data})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _view(
    tool_call_id: str, entry: _Entry, status: DeferredStatus, ended_at: str | None
) -> DeferredEntry:
    return DeferredEntry(
        tool_call_id, entry.kind, entry.label, status, entry.started_at, ended_at
    )


def deliver_background_result(
    task_id: int,
    *,
    tool_call_id: str | None,
    tool_name: str,
    result: dict[str, object],
    placeholder_display_data: dict[str, object] | None = None,
) -> bool:
    """把后台工作的结论作为 ``background_result`` 投递给 Task，返回是否已入队。

    会同步写 SQLite，调用方不得在终端 PTY 读线程等热路径上直接调用。登记过的条目必须经
    ``DeferredRegistry.complete`` 投递（受关闭门控）；直接调用只用于不对应登记的启动期重启结论
    （``restart_settlement``）。
    停止期间的结论由 ``DeferredRegistry.complete`` 在锁内拦下，这里不再复查取消信号。
    Task 已删除时入队抛 ``KeyError``，在此记录并返回 False。
    ``placeholder_display_data`` 是作业给占位卡片的终态展示（见 ``DeferredConclusion``），随消息持久化、
    并入时写回卡片：结论可能早于占位行写入就到，进程内暂存又扛不住重启，所以只能跟着消息走。
    """

    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    data = {"task_id": task_id, "tool_call_id": tool_call_id, "tool_name": tool_name}
    body = BackgroundResultBody(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_result=json.dumps(result, ensure_ascii=False),
        placeholder_display_data=placeholder_display_data,
    )
    try:
        task_runtime_spaces.enqueue(task_id, TaskMessage.of(body))
    except KeyError:
        log.info(
            "background_result_dropped_task_missing",
            extra={"msg": "Task 已删除，丢弃后台结论", "data": data},
        )
        return False
    log.info("background_result_delivered", extra={"msg": "投递后台结论", "data": data})
    return True


deferred_registry = DeferredRegistry()
