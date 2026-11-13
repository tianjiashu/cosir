"""终端命令的结果登记：一次 ``execute_terminal`` 调用登记一条，结论作为工具结果返回或稍后以
``background_result`` 回投。

真正需要「调用闭合后工作继续、结论稍后回投」的只有终端命令（派生 Task 与 Agent Team 是多 Agent
编排，结论由 ``derived_report`` 汇报），所以这张表归终端工具层。条目以 ``tool_call_id`` 为键，每条
恰好产出一个结论；登记表只记录「结论回投给谁、谁在等、撤销时停哪个作业」，不持有命令本身的执行状态——
命令的进程与运行中观察归 ``terminal_runtime``（``running_commands``，进程退出即移除），登记表活到结论
交付完，两者生命周期不同、各管一段。

归属：后台工作属于 Task，不属于登记它的 Run——Run 可以先结束，结论稍后到达时照常唤醒或开出新 Run；
``wait_messages`` 等的是整个 Task 的后台工作，Task「无事可做」（``TaskActor.wait_quiescent``）也要等
它们全部交付。用户停止 Task 不碰它们（只打断当前驱动）；上游收回、删除与关闭撤销 Task 的全部条目。

core 里有三处要问它（静止判定、``wait_messages``、收回时撤销），依赖方向只允许 ``app.tools → core``，
所以 core 经端口 ``core.tool.background_commands`` 问，lifespan 把这张表注入进去。

事实边界：进程内协调状态，不持久化、不跨重启、重启后不重建；重启收尾见 ``RecoverService``。

两种监听（``mode``）：后台条目的结论由登记表订阅作业、以消息回投；前台条目的工具线程还在等，结论作为
工具结果返回，可被 ``detach`` 改为后台（转到后台，登记表此时接过订阅）。只有后台条目在对话里有占位行
（卡片状态见设计文档「卡片状态」），占位行的结算由调用方完成，登记表不碰 context。

生命周期（每条恰好结束一次，由 ``_entries`` 的 pop 保证）：
- 完成：作业产出结论时经订阅调用 ``complete``，出队与投递在同一入口（pop-once 防重复投递，结局只有
  「投递」与「撤销」两种）；前台条目正常结束时调用 ``settle_foreground``，与 ``detach`` 在同一把锁内
  裁决先后；
- 单停：用户点卡片停止与 Agent 的 ``terminal_stop`` 同经 ``request_cancel``，只停作业、条目留到作业带着
  取消结论收尾，回执经 ``complete`` 投递（只唤醒活动 Run，不开新 Run）；Run 取消时前台等待经
  ``cancel_foreground``，与 ``detach`` 在同一把锁内裁决先后；
- 撤销：撤销该 Task 的全部条目（含更早 Run 登记的）并停下作业，之后到达的 ``complete`` 不投递。上游
  收回经端口调用 ``recall_task``（要等作业带着已收集输出收尾，用来结算卡片）；Task 删除提交后
  ``TaskService`` 调用 ``drop_task``（占位行已随删除消失，不等）。``parent`` 信号在时 ``register``
  拒收，收回之后这次驱动剩下的工具不会再留下条目；撤回回合经 ``withdraw_calls``；
- 关闭：后端关闭时 lifespan 在停 actor、关终端之前调用 ``close``：撤销全部条目并等在途投递落定，
  此后 ``complete`` 一律不投递。关闭期间仍在跑的后台命令被杀后会以结论回调，若不先关闭，
  这些线程会在服务依赖关闭期间写 ``task_messages``，重启后挂起的 Run 先收到过期结论再收到
  ``backend_restarted``。正常完成与单停回执共用在途投递收尾，确保 ``close`` 返回后不再写库。

看板：登记表只回答「哪些后台工作还在跑」（``live_background``）。已结束的工作不在这里：它们的
记录持久化在各自后台占位行的 metadata 上（``BackgroundWork``），看板由 snapshot 投影从对话里读出，
所以跨重启保留、随对话 fork 与撤回。前台条目不进看板：结论作为工具结果返回，它只是一次普通的工具
调用；前台条目只经 ``foreground_calls`` 告诉工具卡片「可以转到后台」，转到后台后才算后台工作。
任何变化都经单线程通知执行器在锁外回调 ``set_listener`` 注入的监听方（lifespan 注入 snapshot 投影，
core 不依赖 transport）。不在调用方线程同步回调：变化可能发生在 HTTP 命令或作业线程里，投影要读
snapshot 工作副本，串到一条线程上既不阻塞调用方，也保证同一 Task 的通知按发生顺序到达。

投递要取 Task 当前 Run、结算占位与写外部队列，这几处依赖都在函数内导入：service 与 transport 在模块
顶层导入本表，顶层反向导入会成环。
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from app.config.logging.logger import log
from app.core.tool.schemas import TOOL_EXECUTE_TERMINAL
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.message_queue.task_message import BackgroundResultBody, TaskMessage
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.models.message_metadata import BackgroundWork
from app.tools.tool_handler.terminal.command_job import CommandConclusion, CommandJob

BackgroundMode = Literal["foreground", "background"]
DropReason = Literal["parent", "withdraw", "shutdown"]
ForegroundOutcome = Literal["settled", "detached", "gone"]
ForegroundCancel = Literal["stopping", "detached", "gone"]
DetachOutcome = Literal["detached", "not_found", "not_foreground"]


@dataclass
class _Entry:
    task_id: int
    work: BackgroundWork
    job: CommandJob
    mode: BackgroundMode
    stop_requested: bool
    # 前台条目的等待线程持有的转后台信号；后台条目为 None。
    detached: threading.Event | None


class CommandResultRegistry:
    """按 ``tool_call_id`` 登记未完成的命令结果；线程安全，任意线程可调用。"""

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
        label: str,
        job: CommandJob,
        detached: threading.Event | None = None,
    ) -> BackgroundWork | None:
        """登记作业的结果去向，返回持久化用的工作记录；被拒收时返回 ``None``，作业已被停下。

        ``detached`` 为 ``None`` 是一开始就在后台的命令：登记表订阅作业结论并回投。前台等待传入它持有的
        转后台信号，转后台与结算在同一把锁内裁决（见 :meth:`detach`）。
        重复调用 ID 是装配缺陷，抛 ValueError。关闭后、或 Task 正被收回（``parent`` 信号在）时的登记
        不入表：收回已撤销了表里的条目，被打断的驱动里还没登记完的工具不能再留下工作。被拒的调用不能
        写后台占位（没有人会结算它），调用方据 ``None`` 按取消返回。
        """
        mode: BackgroundMode = "background" if detached is None else "foreground"
        work = BackgroundWork(kind="terminal_command", label=label, started_at=_now(), run_id=run_id)
        data = {"task_id": task_id, "run_id": run_id, "tool_call_id": tool_call_id, "mode": mode}
        with self._lock:
            if (task_id, tool_call_id) in self._entries:
                raise ValueError(f"background result already registered: {tool_call_id}")
            rejected: DropReason | None = (
                "shutdown" if self._closed
                else "parent" if cancellation_registry.reason(task_id) == "parent"
                else None
            )
            if rejected is None:
                self._entries[task_id, tool_call_id] = _Entry(
                    task_id=task_id, work=work, job=job, mode=mode,
                    stop_requested=False, detached=detached,
                )
                pending = sum(entry.task_id == task_id for entry in self._entries.values())
        if rejected is not None:
            log.info(
                "background_result_rejected",
                extra={
                    "msg": "后端正在关闭或 Task 正被收回，后台结果不登记",
                    "data": {**data, "reason": rejected},
                },
            )
            job.cancel()
            return None
        log.info(
            "background_result_registered",
            extra={
                "msg": "登记后台结果",
                "data": {**data, "label": work.label, "pending": pending},
            },
        )
        if mode == "background":
            self._deliver_on_done(task_id, tool_call_id, job)
        self._notify(task_id)
        # 命令在 handler 通过准入后、交接作业前就可能被点名停止。登记后接过该信号，
        # 工具线程返回时会清理它；此后单停直接按活条目处理。
        if tool_call_cancellation_registry.is_cancelled(task_id, tool_call_id):
            self.request_cancel(task_id, tool_call_id, driving=False)
        return work

    def request_cancel(self, task_id: int, tool_call_id: str, *, driving: bool) -> bool:
        """单停：停下作业，条目留到作业带着取消结论收尾；用户点卡片停止与 Agent 的 ``terminal_stop`` 共用。

        工作还在工具线程、尚未登记时（``driving``）先打工具信号，登记会接过它。
        """
        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return driving and tool_call_cancellation_registry.mark_cancelled(
                    task_id, tool_call_id,
                )
            if entry.stop_requested:
                return False
            entry.stop_requested = True
        entry.job.cancel()
        log.info("background_result_stop_requested", extra={
            "msg": "已请求停止，等待作业交付结论",
            "data": {"task_id": task_id, "tool_call_id": tool_call_id},
        })
        return True

    def complete(self, task_id: int, tool_call_id: str, conclusion: CommandConclusion) -> bool:
        """结束一条登记并把结论投递给它的 Task；已撤销、已完成或已关闭时不投递，返回 False。

        出队与「在途计数」在同一把锁内：``close`` 要么先于出队（不投递），要么等这次投递落定。不看取消
        信号：用户停止不结束后台工作，结论入队后由 loop 在执行落定后照常推进；删除中入队的结论随删除级联
        清掉，删除没提交时留给新 actor 照常处理。收回在打信号后立即撤销，结论恰落在两步之间时仍会入队并
        开出新 Run：窗口只有一次撤销调用，接受。
        会同步写 SQLite，调用方不得在 PTY 读线程等热路径上调用。
        """

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None or self._closed:
                return False
            del self._entries[task_id, tool_call_id]
            self._delivering[task_id] += 1
        try:
            if conclusion.cancel_hint is not None:
                self._enqueue_cancel_receipt(task_id, tool_call_id, conclusion)
            else:
                self._enqueue_result(task_id, tool_call_id, conclusion)
        except KeyError:
            # 「Task 已删除」只在这里收敛：取当前 Run 与入队都按 KeyError 报告 Task 不存在。
            log.info(
                "background_result_dropped_task_missing",
                extra={
                    "msg": "Task 已删除，丢弃后台结论",
                    "data": {"task_id": task_id, "tool_call_id": tool_call_id},
                },
            )
            return False
        finally:
            with self._lock:
                self._delivering[task_id] -= 1
                if not self._delivering[task_id]:
                    del self._delivering[task_id]
                self._settled.notify_all()
            self._notify(task_id)
        return True

    def settle_foreground(self, task_id: int, tool_call_id: str) -> ForegroundOutcome:
        """前台等待正常结束：仍是前台则出队（结论作为工具结果返回，不投递），返回 ``settled``。

        与 :meth:`detach` 在同一把锁内裁决：``detached`` 表示转后台先到，结论已由登记表接过的订阅回投，
        调用方返回「已转后台」占位；``gone`` 表示条目已被撤销或关闭，调用方按自己的取消结果返回。
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
        """把前台条目改为后台、置位等待线程持有的信号，并接过结论订阅（结论已到时立即回投）。

        已经要求停止的前台等待（单停或 Run 取消先到）不再转后台：命令正被停下，转过去只会留下一张
        马上取消的后台卡片。
        """

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return "not_found"
            if entry.mode != "foreground" or entry.stop_requested:
                return "not_foreground"
            entry.mode = "background"
            cast(threading.Event, entry.detached).set()
        log.info(
            "background_result_detached",
            extra={
                "msg": "前台等待转到后台",
                "data": {"task_id": task_id, "tool_call_id": tool_call_id},
            },
        )
        self._deliver_on_done(task_id, tool_call_id, entry.job)
        self._notify(task_id)
        return "detached"

    def cancel_foreground(self, task_id: int, tool_call_id: str) -> ForegroundCancel:
        """Run 取消时停下前台等待；与 :meth:`detach` 在同一把锁内裁决，已转后台的命令不受 Run 取消影响。

        ``stopping``：仍是前台，作业已停下（或此前已被单停），调用方等它带着取消结论收尾；``detached``：
        转后台先到，调用方返回「已转后台」占位；``gone``：条目已被撤销，作业已由撤销方停下，调用方同样
        等取消结论。
        """

        with self._lock:
            entry = self._entries.get((task_id, tool_call_id))
            if entry is None:
                return "gone"
            if entry.mode == "background":
                return "detached"
            first = not entry.stop_requested
            entry.stop_requested = True
        if first:
            entry.job.cancel()
        return "stopping"

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

    def pending(self, task_id: int) -> list[str]:
        """返回 Task 仍在后台执行的工作标签（含更早 Run 登记的），供 ``wait_messages`` 判定与提示。"""

        with self._lock:
            return [
                entry.work.label for entry in self._entries.values()
                if entry.task_id == task_id and entry.mode == "background"
            ]

    def has_work(self, task_id: int) -> bool:
        """Task 还有没交付完的工作：有登记条目，或有已出队、正在写入消息队列的结论。

        供 Task 静止判定：只看条目不够，结论出队到入队之间条目已不在、消息还没进队列，这时报静止会让
        等待方读走上一次 Run 的结论。变化经 ``set_listener`` 的监听方通知（见模块说明）。
        """

        with self._lock:
            return bool(self._delivering[task_id]) or any(
                owner == task_id for owner, _ in self._entries
            )

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
                if owner == task_id and entry.mode == "foreground"
            ]

    def drop_task(self, task_id: int) -> None:
        """Task 删除提交后撤销它的全部条目并停下作业；不等作业收尾（占位行已随删除消失，没有要结算的）。

        停作业的异常记录后继续，不阻断删除收尾。
        """
        self._take_task(task_id)

    def recall_task(self, task_id: int) -> dict[str, dict[str, object] | None]:
        """上游收回：撤销 Task 的全部条目并停下作业，返回被撤销的后台调用及其卡片终态展示。

        在锁外等后台作业带着已收集输出的取消结论收尾，供调用方结算原占位；不向 Agent 投递消息。等待
        有上限：执行后端对任何停止请求只限时等收尾（见 ``LocalExecutionBackend``）。会阻塞，须在事件
        循环外调用。
        """
        return {
            call_id: cast(CommandConclusion, entry.job.wait(None)).display_data
            for call_id, entry in self._take_task(task_id)
            if entry.mode == "background"
        }

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

    def _take_task(self, task_id: int) -> list[tuple[str, _Entry]]:
        """移走 Task 的全部条目并在锁外停下它们的作业。"""
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
        return dropped

    def _deliver_on_done(self, task_id: int, tool_call_id: str, job: CommandJob) -> None:
        """订阅后台条目的结论并回投；须在锁外调用（结论已到时立即回调 :meth:`complete`）。"""
        def deliver(conclusion: CommandConclusion) -> None:
            self.complete(task_id, tool_call_id, conclusion)

        job.on_done(deliver)

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
        """锁外逐条停下作业；停止失败记录后继续，不阻断撤销或关闭收尾。"""

        for tool_call_id, entry in dropped:
            data = {"task_id": entry.task_id, "tool_call_id": tool_call_id}
            try:
                entry.job.cancel()
            except Exception:
                log.exception(
                    "background_result_cancel_failed",
                    extra={"msg": "撤销后台结果时停止作业失败", "data": data},
                )
            log.info("background_result_dropped", extra={
                "msg": "撤销后台结果", "data": {**data, "reason": reason},
            })

    @staticmethod
    def _enqueue_cancel_receipt(
        task_id: int, tool_call_id: str, conclusion: CommandConclusion,
    ) -> None:
        """单停的回执：交给 Task 当前的活动 Run（不论是哪个 Run 登记的），但不开新 Run——用户刚停掉
        一项工作，不该因此让 Agent 自己动起来。

        Task 空闲时卡片立即落定（用户点了停止，卡片不能等到下一个 Run 才变），回执照样入队、随下一次推进
        并入，Agent 因此知道这项工作被停了；并入时占位已不是 ``pending``，不会重复结算。"""
        from app.core.tool.placeholder_settlement import cancel_placeholders
        from app.service.depends import get_task_service

        hint = cast(str, conclusion.cancel_hint)
        current = get_task_service().require_current_run(task_id)
        if current is None or current.status not in ACTIVE_RUN_STATUSES:
            cancel_placeholders(
                task_id, [tool_call_id], hint, displays={tool_call_id: conclusion.display_data},
            )
        CommandResultRegistry._enqueue_result(task_id, tool_call_id, conclusion, starts_run=False)

    @staticmethod
    def _enqueue_result(
        task_id: int, tool_call_id: str, conclusion: CommandConclusion, *, starts_run: bool = True,
    ) -> None:
        """把结论作为 ``background_result`` 写进 Task 的外部队列。

        会同步写 SQLite。Task 已删除时入队抛 ``KeyError``（由 :meth:`complete` 收敛）。卡片终态展示随消息
        持久化、并入时写回卡片：结论可能早于占位行写入就到，进程内暂存又扛不住重启，所以只能跟着消息走。
        """
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        data = {"task_id": task_id, "tool_call_id": tool_call_id}
        body = BackgroundResultBody(
            tool_call_id=tool_call_id,
            tool_name=TOOL_EXECUTE_TERMINAL,
            tool_result=json.dumps(conclusion.result, ensure_ascii=False),
            placeholder_display_data=conclusion.display_data,
            placeholder_status="cancelled" if conclusion.cancel_hint else "completed",
            placeholder_error=conclusion.cancel_hint, starts_run=starts_run,
            ended_at=_now(),
        )
        task_runtime_spaces.enqueue(task_id, TaskMessage.of(body))
        log.info("background_result_delivered", extra={"msg": "投递后台结论", "data": data})


def _now() -> str:
    return datetime.now(UTC).isoformat()


command_results = CommandResultRegistry()
