"""Session 推送的进程内通道：侧栏、命令面板、页头与工作台标签共用一条连接，送两类事实。

- **状态**：Session 状态是两项正交的事实（见 ``SessionStatusQueries``）：执行状态的事实源是
  ``RunModel.status``（经 Session 当前 Run 指针读取），等你操作的事实源是 HITL 等待占位行。
  在 Run 开始、状态变化或工具调用状态迁移时现读并转发。
- **目录失效**：侧栏目录（工作区列表、各工作区的侧栏 Session）的写入方在提交后通知「这块目录变了」，
  前端据此重读目录接口。只推失效、不推内容：目录接口仍是唯一事实源，这里不复制目录投影。前端的
  目录刷新只来自这里（外加首次加载与重连后的整体重读），不再由页面事件推断，所以后台会话的
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
from app.models.session_kind import USER_FACING_SESSION_TYPES, SessionType
from app.storage.crud.session_crud import SessionCrud
from app.storage.queries.session_status_queries import SessionStatus, SessionStatusQueries
from app.storage.store_engines import main_db_factory

SessionStatuses = dict[int, SessionStatus]


@dataclass(frozen=True)
class CatalogChange:
    """一批目录失效：工作区列表变了（前端整体重读），或这些工作区的侧栏 Session 变了。"""

    workspaces: bool = False
    workspace_ids: frozenset[int] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return self.workspaces or bool(self.workspace_ids)


@dataclass(frozen=True)
class SessionFeedBatch:
    statuses: SessionStatuses
    catalog: CatalogChange


class SessionFeedSubscription:
    """一个订阅者待发送的变化：同一 Session 的状态只留最新值、目录失效按工作区去重，慢消费者不会积压。

    只在所属事件循环线程上读写（发布方经 ``call_soon_threadsafe`` 投递）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.changed = asyncio.Event()
        self._statuses: SessionStatuses = {}
        self._workspaces = False
        self._workspace_ids: set[int] = set()

    def offer_statuses(self, statuses: SessionStatuses) -> None:
        self._statuses.update(statuses)
        self.changed.set()

    def offer_catalog(self, change: CatalogChange) -> None:
        self._workspaces = self._workspaces or change.workspaces
        self._workspace_ids.update(change.workspace_ids)
        self.changed.set()

    def take(self) -> SessionFeedBatch:
        batch = SessionFeedBatch(
            self._statuses, CatalogChange(self._workspaces, frozenset(self._workspace_ids))
        )
        self._statuses, self._workspaces, self._workspace_ids = {}, False, set()
        self.changed.clear()
        return batch


class SessionFeed:
    """进程级单例，由 ``app.service.depends.get_session_feed`` 装配。

    读库对象在第一次真正读取时才创建：投影路径（含不初始化存储的单测）每个 Run 事件都会发布，
    没有订阅者时不应碰存储引擎。各 ``publish_*`` 可在任意线程调用，调用方须在自己的事务提交之后
    调用（推送出去的失效会立刻引来前端重读）。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._subscriptions: set[SessionFeedSubscription] = set()
        self._session_reads: SessionStatusQueries | None = None
        self._session_crud: SessionCrud | None = None

    @property
    def _reads(self) -> SessionStatusQueries:
        if self._session_reads is None:
            self._session_reads = SessionStatusQueries(main_db_factory())
        return self._session_reads

    @property
    def _sessions(self) -> SessionCrud:
        if self._session_crud is None:
            self._session_crud = SessionCrud()
        return self._session_crud

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> SessionFeedSubscription:
        subscription = SessionFeedSubscription(loop)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: SessionFeedSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    def statuses(self) -> SessionStatuses:
        """当前全部 Session 的状态（整表帧与周期对账）。"""
        return self._reads.list_statuses()

    def publish_status(self, session_id: int) -> None:
        """Session 状态可能变化后调用（Run 开始或迁移、工具调用状态迁移、撤回回合）：现读该 Session 的
        状态并投给全部订阅者。

        没有订阅者时不读库；Session 已被删除时不发（侧栏随目录失效移除它）。
        """
        subscriptions = self._snapshot()
        if not subscriptions:
            return
        try:
            status = self._reads.status(session_id)
        except KeyError:
            return
        self._deliver(subscriptions, lambda subscription: subscription.offer_statuses({session_id: status}))

    def publish_catalog(self, workspace_id: int, session_type: SessionType) -> None:
        """一个 Session 的目录行变了（建、删、改标题、开新 Run 刷新排序时间）。

        只有侧栏会话（``USER_FACING_SESSION_TYPES``）进目录：委派子 Agent 的变化不引起
        目录重读。规则只在这一处，写入方不各自判断。
        """
        if session_type not in USER_FACING_SESSION_TYPES:
            return
        change = CatalogChange(workspace_ids=frozenset({workspace_id}))
        self._deliver(self._snapshot(), lambda subscription: subscription.offer_catalog(change))

    def publish_session_catalog(self, session_id: int) -> None:
        """同 :meth:`publish_catalog`，给只持有 Session id 的事件路径（Run 开始）：有订阅者时才读库，
        Session 已被删除时不发（删除方自己发过）。"""
        if not self._snapshot():
            return
        try:
            session = self._sessions.get(session_id)
        except KeyError:
            return
        self.publish_catalog(session.workspace_id, session.session_type)

    def publish_workspaces(self) -> None:
        """工作区列表变了（建、删工作区）：前端整体重读目录。"""
        change = CatalogChange(workspaces=True)
        self._deliver(self._snapshot(), lambda subscription: subscription.offer_catalog(change))

    def _snapshot(self) -> tuple[SessionFeedSubscription, ...]:
        with self._lock:
            return tuple(self._subscriptions)

    @staticmethod
    def _deliver(
        subscriptions: tuple[SessionFeedSubscription, ...],
        offer: Callable[[SessionFeedSubscription], None],
    ) -> None:
        for subscription in subscriptions:
            try:
                subscription.loop.call_soon_threadsafe(offer, subscription)
            except RuntimeError:
                # 订阅者的事件循环已关闭：连接正在收尾，交给它自己的 finally 退订。
                log.debug(
                    "session_feed_subscriber_closed",
                    extra={"msg": "Session 推送订阅者的事件循环已关闭", "data": {}},
                )
