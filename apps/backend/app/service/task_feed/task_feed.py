"""Task 推送的进程内通道：侧栏、命令面板、页头与工作台标签共用一条连接，送两类事实。

- **状态**：Task 状态是两项正交的事实（见 ``TaskStatusQueries``）：执行状态的事实源是
  ``ConversationRunModel.status``（经 Task 当前 Run 指针读取），等你操作的事实源是 HITL 等待占位行。
  在 Run 开始、状态变化或工具调用状态迁移时现读并转发。
- **目录失效**：侧栏目录（工作区列表、各工作区的侧栏 Task）的写入方在提交后通知「这块目录变了」，
  前端据此重读目录接口。只推失效、不推内容：目录接口仍是唯一事实源，这里不复制目录投影。前端的
  目录刷新只来自这里（外加首次加载与重连后的整体重读），不再由页面事件推断，所以后台 Task 的
  分叉、标题与排序变化也能及时出现在侧栏。

这里不保存事实。漏掉的事件（订阅与整表读取交错、进程内投递失败、连接断开期间）由订阅端收敛：
状态靠流周期重发的整表，目录靠前端重连后的整体重读；不在这里为低概率时序叠加防御。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from app.config.logging.logger import log
from app.models.task_kind import USER_FACING_TASK_TYPES, TaskType
from app.storage.crud.task_crud import TaskCrud
from app.storage.queries.task_status_queries import TaskStatus, TaskStatusQueries
from app.storage.store_engines import main_session_factory

TaskStatuses = dict[int, TaskStatus]


@dataclass(frozen=True)
class CatalogChange:
    """一批目录失效：工作区列表变了（前端整体重读），或这些工作区的侧栏 Task 变了。"""

    workspaces: bool = False
    workspace_ids: frozenset[int] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return self.workspaces or bool(self.workspace_ids)


@dataclass(frozen=True)
class TaskFeedBatch:
    statuses: TaskStatuses
    catalog: CatalogChange


class TaskFeedSubscription:
    """一个订阅者待发送的变化：同一 Task 的状态只留最新值、目录失效按工作区去重，慢消费者不会积压。

    只在所属事件循环线程上读写（发布方经 ``call_soon_threadsafe`` 投递）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.changed = asyncio.Event()
        self._statuses: TaskStatuses = {}
        self._workspaces = False
        self._workspace_ids: set[int] = set()

    def offer_statuses(self, statuses: TaskStatuses) -> None:
        self._statuses.update(statuses)
        self.changed.set()

    def offer_catalog(self, change: CatalogChange) -> None:
        self._workspaces = self._workspaces or change.workspaces
        self._workspace_ids.update(change.workspace_ids)
        self.changed.set()

    def take(self) -> TaskFeedBatch:
        batch = TaskFeedBatch(
            self._statuses, CatalogChange(self._workspaces, frozenset(self._workspace_ids))
        )
        self._statuses, self._workspaces, self._workspace_ids = {}, False, set()
        self.changed.clear()
        return batch


class TaskFeed:
    """进程级单例，由 ``app.service.depends.get_task_feed`` 装配。

    读库对象在第一次真正读取时才创建：投影路径（含不初始化存储的单测）每个 Run 事件都会发布，
    没有订阅者时不应碰存储引擎。各 ``publish_*`` 可在任意线程调用，调用方须在自己的事务提交之后
    调用（推送出去的失效会立刻引来前端重读）。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._subscriptions: set[TaskFeedSubscription] = set()
        self._task_reads: TaskStatusQueries | None = None
        self._task_crud: TaskCrud | None = None

    @property
    def _reads(self) -> TaskStatusQueries:
        if self._task_reads is None:
            self._task_reads = TaskStatusQueries(main_session_factory())
        return self._task_reads

    @property
    def _tasks(self) -> TaskCrud:
        if self._task_crud is None:
            self._task_crud = TaskCrud()
        return self._task_crud

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> TaskFeedSubscription:
        subscription = TaskFeedSubscription(loop)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: TaskFeedSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    def statuses(self) -> TaskStatuses:
        """当前全部 Task 的状态（整表帧与周期对账）。"""
        return self._reads.list_statuses()

    def publish_status(self, task_id: int) -> None:
        """Task 状态可能变化后调用（Run 开始或迁移、工具调用状态迁移、撤回回合）：现读该 Task 的
        状态并投给全部订阅者。

        没有订阅者时不读库；Task 已被删除时不发（侧栏随目录失效移除它）。
        """
        subscriptions = self._snapshot()
        if not subscriptions:
            return
        try:
            status = self._reads.status(task_id)
        except KeyError:
            return
        self._deliver(subscriptions, lambda subscription: subscription.offer_statuses({task_id: status}))

    def publish_catalog(self, workspace_id: int, task_type: TaskType) -> None:
        """一个 Task 的目录行变了（建、删、改标题、开新 Run 刷新排序时间）。

        只有侧栏对话（``USER_FACING_TASK_TYPES``）进目录：委派子 Agent 与 Team 节点的变化不引起
        目录重读。规则只在这一处，写入方不各自判断。
        """
        if task_type not in USER_FACING_TASK_TYPES:
            return
        change = CatalogChange(workspace_ids=frozenset({workspace_id}))
        self._deliver(self._snapshot(), lambda subscription: subscription.offer_catalog(change))

    def publish_task_catalog(self, task_id: int) -> None:
        """同 :meth:`publish_catalog`，给只持有 Task id 的事件路径（Run 开始）：有订阅者时才读库，
        Task 已被删除时不发（删除方自己发过）。"""
        if not self._snapshot():
            return
        try:
            task = self._tasks.get(task_id)
        except KeyError:
            return
        self.publish_catalog(task.workspace_id, task.task_type)

    def publish_workspaces(self) -> None:
        """工作区列表变了（建、删工作区）：前端整体重读目录。"""
        change = CatalogChange(workspaces=True)
        self._deliver(self._snapshot(), lambda subscription: subscription.offer_catalog(change))

    def _snapshot(self) -> tuple[TaskFeedSubscription, ...]:
        with self._lock:
            return tuple(self._subscriptions)

    @staticmethod
    def _deliver(
        subscriptions: tuple[TaskFeedSubscription, ...],
        offer: Callable[[TaskFeedSubscription], None],
    ) -> None:
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(offer, subscription)
            except RuntimeError:
                # 订阅者的事件循环已关闭：连接正在收尾，交给它自己的 finally 退订。
                log.debug(
                    "task_feed_subscriber_closed",
                    extra={"msg": "Task 推送订阅者的事件循环已关闭", "data": {}},
                )
