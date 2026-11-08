"""交接登记表：Agent 把一个资源（终端会话、浏览器标签）暂时交给用户、等用户交还的进行中事实。

交接 = HITL（``execute`` 类）+ 资源租借。「是否还在等用户决定」的事实源仍是等待占位行（见
``ToolDecisionService``），登记表只承载 HITL 不知道的那一半：交给用户的是哪个资源、为什么、交还时
工具要接着报告什么（``memo``）。以 ``tool_call_id`` 为键，同一资源同一时刻至多一条。

所有结束路径都经这里，资源一侧不再各自记一份交接状态：
- 用户交还：决定到达后 ``prepare_input`` 重放原调用，工具调用 :meth:`HandoffRegistry.end` 取回登记；
- 资源在交接中结束（Agent 不知道的结束，如 shell 退出）：资源一侧调用 :meth:`settle`，代用户驳回那次
  调用，唤醒等它的 Agent；
- 撤回这一回合：撤回方调用 :meth:`drop`（否则资源会一直显示交接中，也无法再次交接）；
- Task 删除提交后：``TaskService`` 调用 :meth:`drop_task`。
Run 停止不结束交接：等待中的决定按 HITL 契约在 Run 停止后仍可作出。

为什么放在 core：撤回与 Task 删除在 core 之外的应用层，资源实现在 ``app.tools``；依赖方向只允许
``app.tools → core``，登记表放在双方都能依赖的这一层。资源类型（``HandoffSurface``）是前端据以把
交接提示放到对应面板的标识，新增一种可交接的资源只需加一个取值。

事实边界：进程内协调状态，不持久化、不跨重启、重启后不重建——等待中的决定照常保留，工具卡片上的
交还入口仍在，重放时资源已不在由工具如实报告。变化经单线程通知执行器在锁外回调监听方（lifespan
注入 snapshot 投影），理由同 ``background_registry``：登记常发生在资源锁内，而投影可能反过来读资源。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal

from app.config.logging.logger import log

HandoffSurface = Literal["terminal", "browser"]


@dataclass(frozen=True)
class HandoffLease:
    """一次进行中的交接。

    ``memo`` 是工具自己在交还时要用的数据（如终端交接时的末行号），登记表不解读。
    """

    tool_call_id: str
    task_id: int
    tool_name: str
    surface: HandoffSurface
    resource_id: str
    reason: str
    memo: object = None


class HandoffConflictError(Exception):
    """同一资源已经交给用户（另一次调用的交接还没交还）。"""

    def __init__(self, existing: HandoffLease) -> None:
        super().__init__(f"{existing.surface} {existing.resource_id} is already handed to the user")
        self.existing = existing


class HandoffRegistry:
    """线程安全，任意线程可调用；登记表锁内从不回调资源或监听方。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[tuple[int, str], HandoffLease] = {}
        self._listener: Callable[[int], None] | None = None
        self._notifier = ThreadPoolExecutor(max_workers=1, thread_name_prefix="handoff-notify")

    def set_listener(self, listener: Callable[[int], None] | None) -> None:
        """注入变化监听方（按 task_id 回调，在通知线程、锁外执行）；``None`` 解除。"""

        with self._lock:
            self._listener = listener

    def begin(self, lease: HandoffLease) -> None:
        """登记交接。资源可以在持自己的锁时调用，以便与资源结束原子地裁决先后。

        异常:
            HandoffConflictError: 这个资源已有进行中的交接。
        """

        with self._lock:
            existing = self._on(lease.surface, lease.resource_id)
            if existing is not None:
                raise HandoffConflictError(existing)
            self._leases[lease.task_id, lease.tool_call_id] = lease
        self._log("handoff_started", "Agent 把资源交给用户", lease)
        self._notify(lease.task_id)

    def end(self, task_id: int, tool_call_id: str) -> HandoffLease | None:
        """交还：取出这次调用的登记。

        已被撤回、代为落定或重启丢失时返回 ``None``，重放照常收尾。
        """

        lease = self._pop(task_id, tool_call_id)
        if lease is not None:
            self._log("handoff_returned", "用户交还资源", lease)
        return lease

    def settle(self, task_id: int, tool_call_id: str, feedback: str) -> None:
        """资源在交接中结束：取出登记并代用户驳回那次调用，唤醒等它的 Agent（会写 SQLite）。

        用户可能恰好已经交还：决定已提交或已不在等待时只记录，不重复。资源一侧在锁外调用。
        """

        from app.core.task_actor import SessionConflictError
        from app.service import depends

        lease = self._pop(task_id, tool_call_id)
        if lease is None:
            return
        self._log("handoff_settled", "资源在交接中结束，代用户落定", lease)
        try:
            depends.get_tool_decision_service().submit(
                task_id, tool_call_id, "reject", feedback=feedback, source="runtime"
            )
        except SessionConflictError as exc:
            log.info(
                "handoff_settle_skipped",
                extra={
                    "msg": "交接已有决定或已不在等待，跳过代为落定",
                    "data": {
                        "task_id": lease.task_id,
                        "tool_call_id": tool_call_id,
                        "code": exc.code,
                    },
                },
            )

    def drop(self, task_id: int, tool_call_ids: Iterable[str]) -> None:
        """撤回回合：静默丢弃这些调用的登记，不落定——等待占位已随这一回合移出。"""

        with self._lock:
            dropped = [
                self._leases.pop((task_id, call_id))
                for call_id in tool_call_ids
                if (task_id, call_id) in self._leases
            ]
        for lease in dropped:
            self._log("handoff_dropped", "交接所在的一轮被撤回", lease)
        if dropped:
            self._notify(task_id)

    def drop_task(self, task_id: int) -> None:
        """Task 删除提交后丢弃它的全部登记；不通知（Task 已没有快照可投影）。"""

        with self._lock:
            for key in [key for key in self._leases if key[0] == task_id]:
                del self._leases[key]

    def close(self) -> None:
        """后端关闭时清空：登记表本来就不跨重启。

        同一进程内 lifespan 可能多次运行（测试），不能残留。
        """

        with self._lock:
            self._leases.clear()

    def on_resource(self, surface: HandoffSurface, resource_id: str) -> HandoffLease | None:
        with self._lock:
            return self._on(surface, resource_id)

    def leases(self, task_id: int) -> list[HandoffLease]:
        """本 Task 进行中的交接，按登记先后。"""

        with self._lock:
            return [lease for lease in self._leases.values() if lease.task_id == task_id]

    def _on(self, surface: HandoffSurface, resource_id: str) -> HandoffLease | None:
        return next(
            (
                lease
                for lease in self._leases.values()
                if lease.surface == surface and lease.resource_id == resource_id
            ),
            None,
        )

    def _pop(self, task_id: int, tool_call_id: str) -> HandoffLease | None:
        with self._lock:
            lease = self._leases.pop((task_id, tool_call_id), None)
        if lease is not None:
            self._notify(lease.task_id)
        return lease

    def _notify(self, task_id: int) -> None:
        with self._lock:
            listener = self._listener
        if listener is None:
            return

        def run() -> None:
            try:
                listener(task_id)
            except Exception:
                log.exception(
                    "handoff_listener_failed",
                    extra={"msg": "交接变化通知失败", "data": {"task_id": task_id}},
                )

        self._notifier.submit(run)

    @staticmethod
    def _log(event: str, msg: str, lease: HandoffLease) -> None:
        log.info(
            event,
            extra={
                "msg": msg,
                "data": {
                    "task_id": lease.task_id,
                    "tool_call_id": lease.tool_call_id,
                    "surface": lease.surface,
                    "resource_id": lease.resource_id,
                },
            },
        )


handoff_registry = HandoffRegistry()

__all__ = [
    "HandoffConflictError",
    "HandoffLease",
    "HandoffRegistry",
    "HandoffSurface",
    "handoff_registry",
]
