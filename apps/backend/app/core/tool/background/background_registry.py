"""后台结果登记表：一次工具调用登记一条、结论稍后以 ``background_result`` 回投的工作。

条目以 ``tool_call_id`` 为键，每条恰好产出一个
结论；登记表只记录「结论回投给谁、谁在等、撤销时通知谁」，不持有工作本身的执行状态——工作的
生命周期归各执行单元（终端 runtime 等），登记表不是任何一种 Run 的第二套状态机。

为什么放在 core：``wait_messages`` 判定是否还有可等待的工作、``TaskActor`` 在 Run 停止时撤销条目，
都在 core；而具体的工作在 ``app.tools`` 一侧实现。依赖方向只允许 ``app.tools → core``，所以由
执行单元显式 ``register`` 并传入撤销回调（依赖倒置），core 不 import 工具层。

事实边界：进程内协调状态，不持久化、不跨重启、重启后不重建；重启收尾见 ``RecoverService``。

两种监听（``mode``）：后台条目的结论以消息回投；前台条目的工具线程还在等，结论作为工具结果返回，
可被 ``detach`` 改为后台（转到后台）。只有后台条目在对话里有占位行（卡片状态见设计文档「卡片
状态」），占位行的结算由调用方完成，登记表不碰 context。

生命周期（每条恰好结束一次，由 ``_entries`` 的 pop 保证）：
- 完成：执行单元产出结论时调用 ``complete``，出队与投递在同一入口（pop-once 防重复投递）；Task
  正在停止时 ``complete`` 不出队，条目由随后的撤销结束（结局只有「投递」与「撤销」两种）；前台
  条目正常结束时调用 ``settle_foreground``，与 ``detach`` 在同一把锁内裁决先后；
- 撤销：Run 停止（用户停止、父 Task 停止）时 ``TaskCancellation`` 调用 ``drop_task``，Task 删除
  提交后 ``TaskService`` 同样调用，撤销该 Task 的全部条目（含更早 Run 登记的）并逐条调用撤销回调，
  之后到达的 ``complete`` 不投递；用户单停经 ``request_user_cancel``，编辑撤回经 ``withdraw``；
- 关闭：后端关闭时 lifespan 在停 actor、关终端之前调用 ``close``：撤销全部条目并等在途投递落定，
  此后 ``complete`` 一律不投递。关闭期间仍在跑的后台命令被杀后会以结论回调，若不先关闭，
  这些线程会在服务依赖关闭期间写 ``task_messages``，重启后挂起的 Run 先收到过期结论再收到
  ``backend_restarted``。正常完成与单项取消共用在途投递收尾，确保 ``close`` 返回后不再写库。

看板：登记表只回答「哪些后台工作还在跑」（``live_background``）。已结束的工作不在这里：它们的
记录持久化在各自后台占位行的 metadata 上（``BackgroundWork``），看板由 snapshot 投影从对话里读出，
所以跨重启保留、随对话 fork 与撤回。前台条目不进看板：结论作为工具结果返回，它只是一次普通的工具
调用；前台条目只经 ``foreground_calls`` 告诉工具卡片「可以转到后台」，转到后台后才算后台工作。
任何变化都经单线程通知执行器在锁外回调
``set_listener`` 注入的监听方（lifespan 注入 snapshot 投影，core 不依赖 transport）。不能在调用方
线程同步回调：登记常发生在终端会话锁内，而投影可能反过来读终端会话。
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.config.logging.logger import log
from app.core.tool.background.background_job import BackgroundConclusion
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.message_queue.task_message import BackgroundResultBody, TaskMessage
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.models.message_metadata import BackgroundKind, BackgroundWork

# 工作种类与原始标签由 BackgroundWork 持有；登记表只额外描述进程内执行方式。
BackgroundMode = Literal["foreground", "background"]
DropReason = Literal["user", "parent", "withdraw", "shutdown"]
ForegroundOutcome = Literal["settled", "detached", "gone"]
DetachOutcome = Literal["detached", "not_found", "not_foreground"]


@dataclass
class _Entry:
    task_id: int
    work: BackgroundWork
    mode: BackgroundMode
    on_cancel: Callable[[DropReason], None]
    after_commit: Callable[[], None] | None
    reports_user_stop: bool
    stop_requested: bool
    detached: threading.Event | None
    collect_cancelled: Callable[[], BackgroundConclusion] | None


class BackgroundRegistry:
    """按 ``tool_call_id`` 登记未完成的后台结果；线程安全，任意线程可调用。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        # 调用 id 只在 Task 内唯一（见 ``RuntimeMessageContextManager``），两个 Task 可以同号。
        self._entries: dict[tuple[int, str], _Entry] = {}
        self._closed = False
        # 各 Task 已出队、还在锁外写消息的投递数：关闭等全部写完，撤回一轮等这个 Task 的写完。
        self._delivering: Counter[int] = Counter()
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
        kind: BackgroundKind,
        label: str,
        on_cancel: Callable[[DropReason], None],
        after_commit: Callable[[], None] | None = None,
        reports_user_stop: bool = False,
        mode: BackgroundMode = "background",
        detached: threading.Event | None = None,
        collect_cancelled: Callable[[], BackgroundConclusion] | None = None,
    ) -> BackgroundWork:
        """登记作业的结果去向，返回持久化用的工作记录。

        ``after_commit`` 接收批准回执已提交的通知；``reports_user_stop`` 表示单项停止的结果
        由作业收尾后交付。撤回、父停止与关闭总是先移走条目，再通知作业。
        ``collect_cancelled`` 由能保证停止收尾的执行器提供：父停止在锁外取唯一结论，
        用于原占位展示结算，不投递消息；收尾时限只归执行器。
        前台等待传入 ``detached``，转后台与结算在同一把锁内裁决。
        重复调用 ID 是装配缺陷，抛 ValueError。关闭后的登记立即通知作业退出。
        """
        work = BackgroundWork(kind=kind, label=label, started_at=_now(), run_id=run_id)
        data = {
            "task_id": task_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "kind": work.kind,
            "mode": mode,
        }
        with self._lock:
            if (task_id, tool_call_id) in self._entries:
                raise ValueError(f"background result already registered: {tool_call_id}")
            closed = self._closed
            if not closed:
                self._entries[task_id, tool_call_id] = _Entry(
                    task_id=task_id, work=work, mode=mode, on_cancel=on_cancel,
                    after_commit=after_commit, reports_user_stop=reports_user_stop,
                    stop_requested=False, detached=detached, collect_cancelled=collect_cancelled,
                )
                pending = sum(entry.task_id == task_id for entry in self._entries.values())
        if closed:
            log.info(
                "background_result_rejected_closed",
                extra={"msg": "后端正在关闭，后台结果不登记", "data": data},
            )
            on_cancel("shutdown")
            return work
        log.info(
            "background_result_registered",
            extra={
                "msg": "登记后台结果",
                "data": {**data, "label": work.label, "pending": pending},
            },
        )
        self._notify(task_id)
        # 命令在 handler 通过准入后、交接作业前就可能被点名停止。登记后接过该信号，
        # 工具线程返回时会清理它；此后单停直接按活条目处理。
        if tool_call_cancellation_registry.is_cancelled(task_id, tool_call_id):
            self.request_user_cancel(task_id, tool_call_id, driving=False)
        return work

    def commit_start(self, task_id: int, tool_call_id: str) -> None:
        """审批启动回执已提交，放行作业。撤销先到时它已由取消回调收尾。"""
        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return
            start, entry.after_commit = entry.after_commit, None
        if start is not None:
            start()

    def request_user_cancel(self, task_id: int, tool_call_id: str, *, driving: bool) -> bool:
        """在与登记相同的锁里选择停止对象。作业自行报告时保留条目直到实际收尾。

        工作还在工具线程时先打工具信号，登记会接过它；普通后台作业撤销后立即投递取消回执。
        """
        from app.core.tool.background.cancel_placeholders import USER_CANCELLED_HINT

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return driving and tool_call_cancellation_registry.mark_cancelled(
                    task_id, tool_call_id,
                )
            if entry.stop_requested:
                return False
            entry.stop_requested = True
            if not entry.reports_user_stop:
                if entry.mode == "background":
                    self._take_for_delivery_locked(task_id, tool_call_id)
                else:
                    del self._entries[task_id, tool_call_id]
        if entry.reports_user_stop:
            entry.on_cancel("user")
            log.info("background_result_stop_requested", extra={
                "msg": "已请求停止，等待作业交付结论",
                "data": {"task_id": task_id, "tool_call_id": tool_call_id},
            })
            return True
        if entry.mode == "background":
            self._deliver(
                tool_call_id, entry,
                BackgroundConclusion(
                    {
                        "end_reason": "cancelled",
                        "message": "The user cancelled this background call.",
                    },
                    cancel_hint=USER_CANCELLED_HINT,
                ),
                cancel_job=True,
            )
        else:
            self._cancel_all([(tool_call_id, entry)], "user")
            self._notify(task_id)
        return True

    def withdraw_uncommitted(self, task_id: int, run_id: int) -> list[str]:
        """执行异常退出时撤回未提交启动回执的作业；已放行的工作继续正常回投。"""
        with self._lock:
            dropped = [
                (call_id, item) for (owner, call_id), item in self._entries.items()
                if owner == task_id and item.work.run_id == run_id
                and item.after_commit is not None
            ]
            for call_id, _ in dropped:
                del self._entries[task_id, call_id]
        self._cancel_all(dropped, "withdraw")
        if dropped:
            self._notify(task_id)
        return [call_id for call_id, _ in dropped]

    def complete(self, task_id: int, tool_call_id: str, conclusion: BackgroundConclusion) -> bool:
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
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None or self._closed:
                return False
            if cancellation_registry.is_cancelled(task_id):
                log.info(
                    "background_result_left_for_drop",
                    extra={
                        "msg": "Task 正在停止，结论不投递，条目留给撤销",
                        "data": {"task_id": task_id, "tool_call_id": tool_call_id},
                    },
                )
                return False
            self._take_for_delivery_locked(task_id, tool_call_id)
        return self._deliver(tool_call_id, entry, conclusion)

    def _take_for_delivery_locked(self, task_id: int, tool_call_id: str) -> None:
        """调用方持锁且已确认条目可结束：出队与在途计数一起提交，关闭必须等待其收尾。"""
        del self._entries[task_id, tool_call_id]
        self._delivering[task_id] += 1

    def _deliver(
        self, tool_call_id: str, entry: _Entry, conclusion: BackgroundConclusion,
        *, cancel_job: bool = False,
    ) -> bool:
        """锁外投递已计入在途的结论；单停先通知作业，回调与回执无论成功失败都在此释放计数。"""
        try:
            if cancel_job:
                self._cancel_all([(tool_call_id, entry)], "user")
            if conclusion.cancel_hint is not None:
                return report_cancelled_result(
                    entry.task_id, tool_call_id, entry.work.kind, entry.work.run_id,
                    conclusion.result, conclusion.cancel_hint, conclusion.display_data,
                )
            return deliver_background_result(
                entry.task_id,
                tool_call_id=tool_call_id,
                tool_name=entry.work.kind,
                result=conclusion.result,
                placeholder_display_data=conclusion.display_data,
            )
        finally:
            with self._lock:
                self._delivering[entry.task_id] -= 1
                if not self._delivering[entry.task_id]:
                    del self._delivering[entry.task_id]
                self._settled.notify_all()
            self._notify(entry.task_id)

    def settle_foreground(self, task_id: int, tool_call_id: str) -> ForegroundOutcome:
        """前台等待正常结束：仍是前台则出队（结论作为工具结果返回，不投递），返回 ``settled``。

        与 :meth:`detach` 在同一把锁内裁决：``detached`` 表示转后台先到，调用方须改用
        :meth:`complete` 回投已拿到的结论并返回「已转后台」占位；``gone`` 表示条目已被撤销或关闭，
        调用方按自己的取消结果返回。
        """

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return "gone"
            if entry.mode == "background":
                return "detached"
            del self._entries[task_id, tool_call_id]
        self._notify(task_id)
        return "settled"

    def detach(self, task_id: int, tool_call_id: str) -> DetachOutcome:
        """把前台条目改为后台并同时置位等待线程持有的信号。"""

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return "not_found"
            if entry.mode != "foreground" or entry.detached is None:
                return "not_foreground"
            entry.mode = "background"
            entry.detached.set()
        log.info(
            "background_result_detached",
            extra={
                "msg": "前台等待转到后台",
                "data": {"task_id": task_id, "tool_call_id": tool_call_id},
            },
        )
        self._notify(task_id)
        return "detached"

    def withdraw_calls(self, task_id: int, tool_call_ids: Collection[str]) -> None:
        """撤回回合：放弃这些调用的作业与结论，并等这个 Task 的在途投递写完再返回。

        结论先在锁内出队、再到锁外写消息队列（见 :meth:`complete`）：只撤销登记的话，落在两步之间的
        结论已出队、撤销扑空，随后照常入队，以一条找不到调用的孤儿结论进入新 Run。等在途投递写完，
        调用方随后删除队列里属于这些调用的行就一定能看到它们。会阻塞，须在事件循环与写事务之外调用
        （在途投递要写库）。
        """

        wanted = set(tool_call_ids)
        with self._lock:
            dropped = [
                (call_id, self._entries.pop((task_id, call_id)))
                for call_id in list(wanted)
                if (task_id, call_id) in self._entries
            ]
            while self._delivering[task_id]:
                self._settled.wait()
        self._cancel_all(dropped, "withdraw")
        if dropped:
            self._notify(task_id)

    def withdraw(
        self, task_id: int, tool_call_id: str, *, reason: DropReason = "withdraw"
    ) -> bool:
        """放弃一个作业及其结论；出队后才通知执行者。返回是否实际撤销。"""

        with self._lock:
            entry = self._entries.pop((task_id, tool_call_id), None)
            if entry is None:
                return False
        self._cancel_all([(tool_call_id, entry)], reason)
        self._notify(task_id)
        return True

    def discard(self, task_id: int, tool_call_id: str) -> None:
        """移除一条登记且不调撤销回调：登记方在工作真正启动前失败（如投递入队失败）时收回。"""

        with self._lock:
            entry = self._entries.pop((task_id, tool_call_id), None)
        if entry is not None:
            log.info(
                "background_result_discarded",
                extra={
                    "msg": "工作未启动，收回后台结果登记",
                    "data": {"task_id": task_id, "tool_call_id": tool_call_id},
                },
            )
            self._notify(task_id)

    def pending(self, task_id: int, run_id: int) -> list[str]:
        """返回本 Run 仍在后台执行的工作标签，供 ``wait_messages`` 判定与提示。"""

        with self._lock:
            return [
                entry.work.label for entry in self._entries.values()
                if entry.task_id == task_id and entry.work.run_id == run_id
                and entry.mode == "background"
            ]

    def live_background(self, task_id: int) -> dict[str, BackgroundWork]:
        """Task 下还在跑的后台工作：``tool_call_id`` → 工作记录，按登记先后；前台条目不在其中。

        看板据此判断哪些工作仍在运行（已结束的记录在占位行上，见模块说明）。带完整记录而不只是
        调用 id：并行批次里占位行要等整批结束才写入，这段时间看板只能从这里拿到名字与开始时间。
        """

        with self._lock:
            return {
                tool_call_id: entry.work
                for (owner, tool_call_id), entry in self._entries.items()
                if owner == task_id and entry.mode == "background"
            }

    def foreground_calls(self, task_id: int) -> list[str]:
        """Task 下正在前台等待、可以转到后台的调用（按登记先后），供工具卡片的「转到后台」。"""

        with self._lock:
            return [
                tool_call_id
                for (owner, tool_call_id), entry in self._entries.items()
                if owner == task_id
                and entry.mode == "foreground"
                and entry.detached is not None
            ]

    def drop_task(self, task_id: int) -> dict[str, dict[str, object] | None]:
        """撤销 Task 的全部条目（Run 停止、Task 删除提交后调用）；回调异常记录后继续，不阻断收尾。

        返回需要结算后台占位的调用及最终展示（删除时占位行已不在）。
        所有作业先取消，再在锁外收集支持取消输出的作业结论；不向 Agent 投递消息。
        """

        with self._lock:
            dropped = [
                (tool_call_id, entry)
                for (owner, tool_call_id), entry in self._entries.items()
                if owner == task_id
            ]
            for tool_call_id, _ in dropped:
                del self._entries[task_id, tool_call_id]
        self._cancel_all(dropped, "parent")
        if dropped:
            self._notify(task_id)
        displays: dict[str, dict[str, object] | None] = {}
        for call_id, entry in dropped:
            if entry.mode != "background":
                continue
            displays[call_id] = None
            if entry.collect_cancelled is not None:
                try:
                    displays[call_id] = entry.collect_cancelled().display_data
                except Exception:
                    log.exception("background_cancel_output_failed", extra={
                        "msg": "取消输出收集失败，保留已有预览并结算取消状态",
                        "data": {"task_id": task_id, "tool_call_id": call_id},
                    })
        return displays

    def close(self) -> None:
        """后端关闭：此后不再投递任何结论，撤销全部条目并等在途投递写完。

        先于 actor 与终端关闭调用（见模块说明）；会阻塞到在途投递落定，须在事件循环外调用。
        """

        with self._lock:
            self._closed = True
            dropped = [(call_id, entry) for (_, call_id), entry in self._entries.items()]
            self._entries.clear()
            while self._delivering:
                self._settled.wait()
        self._cancel_all(dropped, "shutdown")

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
                    "background_result_listener_failed",
                    extra={"msg": "后台结果变化通知失败", "data": {"task_id": task_id}},
                )

        self._notifier.submit(run)

    @staticmethod
    def _cancel_all(dropped: list[tuple[str, _Entry]], reason: DropReason) -> None:
        """锁外逐条调用撤销回调；回调异常记录后继续，不阻断取消或关闭收尾。"""

        for tool_call_id, entry in dropped:
            data = {"task_id": entry.task_id, "tool_call_id": tool_call_id, "kind": entry.work.kind}
            try:
                entry.on_cancel(reason)
            except Exception:
                log.exception(
                    "background_result_cancel_failed",
                    extra={"msg": "撤销后台结果的回调失败", "data": data},
                )
            log.info("background_result_dropped", extra={
                "msg": "撤销后台结果", "data": {**data, "reason": reason},
            })


def _now() -> str:
    return datetime.now(UTC).isoformat()



def report_cancelled_result(
    task_id: int, tool_call_id: str, kind: BackgroundKind, run_id: int | None,
    result: dict[str, object], hint: str,
    display_data: dict[str, object] | None = None,
) -> bool:
    """单项停止只唤醒工作所属的活动 Run；已经结束时直接落定历史卡。"""
    from app.core.tool.background.cancel_placeholders import cancel_placeholders
    from app.service.depends import get_task_service

    current = get_task_service().require_current_run(task_id)
    if current is not None and current.id == run_id and current.status in ACTIVE_RUN_STATUSES:
        return deliver_background_result(
            task_id, tool_call_id=tool_call_id, tool_name=kind, result=result,
            cancel_hint=hint, wake_run_id=run_id, placeholder_display_data=display_data,
        )
    cancel_placeholders(task_id, [tool_call_id], hint, displays={tool_call_id: display_data})
    return False


def deliver_background_result(
    task_id: int,
    *,
    tool_call_id: str | None,
    tool_name: str,
    result: dict[str, object],
    placeholder_display_data: dict[str, object] | None = None,
    cancel_hint: str | None = None,
    wake_run_id: int | None = None,
) -> bool:
    """把后台工作的结论作为 ``background_result`` 投递给 Task，返回是否已入队。

    会同步写 SQLite，调用方不得在终端 PTY 读线程等热路径上直接调用。登记过的条目必须经
    ``BackgroundRegistry`` 的完成或单停入口投递，共用关闭等待；重启收尾在登记失效后也经此投递。
    停止期间的结论由 ``BackgroundRegistry.complete`` 在锁内拦下，这里不再复查取消信号。
    Task 已删除时入队抛 ``KeyError``，在此记录并返回 False。
    ``placeholder_display_data`` 是作业给占位卡片的终态展示（见 ``BackgroundConclusion``），
    随消息持久化、并入时写回卡片：结论可能早于占位行写入就到，进程内暂存又扛不住重启，所以只能跟着消息走。
    """

    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    data = {"task_id": task_id, "tool_call_id": tool_call_id, "tool_name": tool_name}
    body = BackgroundResultBody(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_result=json.dumps(result, ensure_ascii=False),
        placeholder_display_data=placeholder_display_data,
        placeholder_status="cancelled" if cancel_hint else "completed",
        placeholder_error=cancel_hint, wake_run_id=wake_run_id,
        ended_at=_now(),
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


background_registry = BackgroundRegistry()
