"""进程内 Assistant Transport 状态与 canonical 冷读边界。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Sequence
from contextlib import suppress
from threading import RLock
from typing import Any, ClassVar, cast

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.user_input_projection import build_presentation_parts
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_background_task import (
    ConversationBackgroundTask,
    project_background_tasks,
)
from app.assistant_transport.state.conversation_state_handoff import (
    ConversationHandoff,
    project_handoff,
)
from app.assistant_transport.state.conversation_state_message import ConversationQueuedMessage
from app.assistant_transport.state.conversation_state_mutation import (
    ConversationStateMutation,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    validate_snapshot,
)
from app.assistant_transport.state.conversation_state_terminal_session import (
    ConversationTerminalSession,
    to_terminal_session,
)
from app.assistant_transport.state.message_identity import user_message_id
from app.assistant_transport.state.snapshot_window import HistoryPage, history_page
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.core.events import RunStatusChangedEvent, ToolCallStatusChangedEvent, WorkflowEvent
from app.core.tool.background.backgraound_registry import background_registry
from app.core.workflow.message_queue.task_message import UserInputBody
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class ConversationTaskStateService:
    """持有进程本地 Transport 状态，并按需从 canonical 记录懒重建。

    工作副本是刻意易失的，挂载在 task 的 ``TaskRuntimeSpace`` 中：projector 变更与 subscriber 变化
    都不写数据库行（展示数据不是事实源，子 Task 状态由应用级任务状态推送承载，不回写委派卡片）。
    某个 task 的首次读取会加载一个 Task、其全部 Run 与全部 context 行，
    再把纯 wire 状态构造委托给 ``ConversationTaskStateRebuilder``；排队消息与终端会话列表不在
    canonical context 里，重建后分别从外部队列与 ``TerminalSessionService`` 整表填入。之后的读取
    复用该 task 级的进程内内存快照，直到显式 rebuild 或进程重置。

    参数:
        task_source: Task 的 CRUD 类数据源；默认取进程依赖。
        run_source: Run 的 CRUD 类数据源；默认取进程依赖。
        context_source: Context 的 CRUD 类数据源；默认取进程依赖。

    副作用:
        ``get_state`` 可能读取三张 canonical SQLite 表，并只缓存一份内存副本。
        ``apply_planned`` 与 ``publish_state`` 更新进程本地状态并通知 SSE subscriber。
        不访问 Run 执行进度、工具 handler 或前端运行时状态；终端会话只通过
        ``TerminalSessionService.task_runtimes`` 读取进程内事实，不回写工具卡片。

    并发:
        ``_lock``（类级 ``RLock``）是每个 ``TaskRuntimeSpace`` 快照工作副本的**唯一串行化点**：
        懒重建、projector 变更、发布、subscriber 注册与删除清理全部经过它。因此 space 自身不
        持有快照锁（见 ``app.task_runtime.task_runtime_space`` 模块 docstring）。直接访问 space
        仅在单线程下合法（状态生命周期测试即如此）；多线程调用方必须自行串行化，推荐做法是改走
        本 service 而非重新给 space 加锁。审查者不应把"缺少 per-space 锁"视为缺陷。
    """

    _lock: ClassVar[RLock] = RLock()
    _subscribers: ClassVar[dict[int, set[Subscriber]]] = {}
    _deleted_task_ids: ClassVar[set[int]] = set()

    def __init__(
        self,
    ) -> None:
        """绑定冷读所需的 canonical 记录数据源。"""
        from app.service import depends

        self._task_source = depends.get_task_crud()
        self._run_source = depends.get_conversation_run_crud()
        self._context_source = depends.get_conversation_task_context_crud()

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """返回已校验的 task 级工作副本，首次访问时懒重建。

        若 ``TaskRuntimeSpace`` 已持有快照则直接返回，不读 canonical 记录。数据库变更通过正常的
        projector/更新路径可见；普通读取不会重建或合并第二份副本。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_snapshot(lambda: self._rebuild(task_id))
            validate_snapshot(state)
            return state

    def get_history_page(self, task_id: int, end: int) -> HistoryPage:
        """完整历史中位置 ``end`` 之前的一页；只拷贝这一页，不拷贝整份工作副本。

        位置与增量路径同一口径（完整历史里的下标），只在完整快照处才会变；``end`` 越界说明
        客户端的窗口来自已被替换的快照。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            ValueError: ``end`` 超出当前历史。
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            return copy.deepcopy(history_page(state["messages"], end))

    def refresh_queued_messages(self, task_id: int) -> None:
        """把排队投影整表同步为外部队列的现状，有变化时发一条 ``set`` mutation。

        排队投影只镜像队列表：入队、撤回（删除、立即发送）之后都调这里，不区分增删。消费投影不从
        这里取内容（事件自带展示绑定），所以确认删除与消费投影之间刷掉这一条也无妨。读队列放在锁内：
        与消费投影串行，先读后提交的旧结果会被随后的消费投影覆盖，不会把已并入的消息留成排队。

        不全量重建：流式输出的 context 可能领先于尚未发出的 Transport delta，重建会提前展示正文
        再重复追加。snapshot 尚未物化时什么也不做，之后的懒重建自会读到队列。
        """
        try:
            with self._lock:
                self._ensure_not_deleted(task_id)
                space = task_runtime_spaces.get_or_create(task_id)
                if not space.has_snapshot():
                    return
                state = space.get_working_snapshot(lambda: self._rebuild(task_id))
                queued = self._queued_messages(task_id)
                if queued == state.get("queued_messages", []):
                    return
                mutation = ConversationStateMutation("set", ("queued_messages",), queued)
                _apply_mutation(state, mutation)
                self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=(mutation,)))
        except Exception as exc:
            log.exception(
                "queued_projection_refresh_failed",
                extra={
                    "msg": "排队投影刷新失败，照常推进；等后续刷新或重建收敛",
                    "data": {"task_id": task_id},
                },
                exc_info=exc,
            )
    @staticmethod
    def _queued_messages(task_id: int) -> list[ConversationQueuedMessage]:
        from app.service.depends import get_task_message_queue

        # 投影只看消息属性；目前需投影的只有用户输入，其它 kind 有可投影形态时在这里加渲染。
        return [
            ConversationQueuedMessage(
                id=user_message_id(cast(int, item.id)),
                role="user",
                parts=list(build_presentation_parts(item.body.presentation)),
                delivery=item.delivery,
            )
            for item in get_task_message_queue().pending_nowait(task_id)
            if item.project_to_transport and isinstance(item.body, UserInputBody)
        ]

    def refresh_terminal_sessions(self, task_id: int) -> None:
        """会话变化通知入口：整表重读该 Task 的终端会话，有变化时发一条 ``set`` mutation。

        由 ``TerminalSessionRuntime.notify_changed`` 经终端副作用执行器调用（非 PTY 读线程、
        锁外），任意线程可调。只更新已物化的快照：未物化时之后的懒重建自会读到最新列表；Task
        已删除或 space 不存在时静默返回——删除收尾关闭会话会触发通知，此时已无人订阅。不用
        ``get_or_create``，否则一次迟到的通知会为已删除或从未打开的 Task 物化 space 与 actor。
        读服务放在锁内，并发通知按序落定，不会以旧列表覆盖新列表。条目由本进程终端服务按契约
        产出，这里不重复校验；契约校验统一在 ``validate_snapshot``（读取快照时执行）。
        """

        with self._lock:
            if task_id in self._deleted_task_ids:
                return
            space = task_runtime_spaces.get(task_id)
            if space is None or not space.has_snapshot():
                return
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            sessions = self._terminal_sessions(task_id)
            if sessions == state["terminal_sessions"]:
                return
            mutation = ConversationStateMutation("set", ("terminal_sessions",), sessions)
            _apply_mutation(state, mutation)
            self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=(mutation,)))

    def refresh_background_results(self, task_id: int) -> None:
        """后台结果变化入口：整表重读看板与前台等待，各自有变化时发 ``set`` mutation（同一帧）。

        由登记表的通知线程调用（锁外），规则同 :meth:`refresh_terminal_sessions`：只更新已物化的
        快照，Task 已删除或 space 不存在时静默返回，不为迟到的通知物化 space。
        """

        with self._lock:
            if task_id in self._deleted_task_ids:
                return
            space = task_runtime_spaces.get(task_id)
            if space is None or not space.has_snapshot():
                return
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            mutations = tuple(
                ConversationStateMutation("set", (key,), value)
                for key, value in (
                    ("background_tasks", self._background_tasks(task_id, state)),
                    ("foreground_waits", self._foreground_waits(task_id)),
                )
                if value != state[key]
            )
            if not mutations:
                return
            for mutation in mutations:
                _apply_mutation(state, mutation)
            self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=mutations))

    def refresh_handoffs(self, task_id: int) -> None:
        """交接变化入口：整表重读该 Task 的进行中交接，有变化时发一条 ``set`` mutation。

        由交接登记表的通知线程调用（锁外），规则同 :meth:`refresh_terminal_sessions`。
        """

        with self._lock:
            if task_id in self._deleted_task_ids:
                return
            space = task_runtime_spaces.get(task_id)
            if space is None or not space.has_snapshot():
                return
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            handoffs = self._handoffs(task_id)
            if handoffs == state["handoffs"]:
                return
            mutation = ConversationStateMutation("set", ("handoffs",), handoffs)
            _apply_mutation(state, mutation)
            self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=(mutation,)))

    @staticmethod
    def _handoffs(task_id: int) -> list[ConversationHandoff]:
        from app.core.tool.handoff.handoff_registry import handoff_registry

        return [project_handoff(lease) for lease in handoff_registry.leases(task_id)]

    @staticmethod
    def _background_tasks(
        task_id: int, state: ConversationStateSnapshot
    ) -> list[ConversationBackgroundTask]:
        """从工作副本里的工具 part 与登记表活条目投影看板；纯内存，可在快照锁内调用。"""
        return project_background_tasks(
            state["messages"], background_registry.live_background(task_id)
        )

    @staticmethod
    def _foreground_waits(task_id: int) -> list[str]:
        from app.core.tool.background.backgraound_registry import background_registry

        return background_registry.foreground_calls(task_id)

    @staticmethod
    def _terminal_sessions(task_id: int) -> list[ConversationTerminalSession]:
        from app.service.depends import get_terminal_session_service

        # 校验统一在 validate_snapshot。
        runtimes = get_terminal_session_service().task_runtimes(task_id)
        return [to_terminal_session(runtime.snapshot()) for runtime in runtimes]

    def rebuild_state(self, task_id: int) -> ConversationStateSnapshot:
        """丢弃进程内 working copy，并立即从 canonical 记录重建该 task 的快照。

        与 ``get_state`` 的区别是**强制**丢弃现有副本：调用方已确认快照缺少 canonical 确实
        存在的 run（二者分叉）时使用，避免继续在陈旧副本上投影。本方法是进程内唯一显式的
        快照自愈入口（``get_state`` 见到已物化副本不会重建，``unload_snapshot`` 此前只被
        删除清理使用）。

        参数:
            task_id: 目标任务标识。

        返回:
            重建后并通过 ``validate_snapshot`` 校验的 task 快照（深拷贝）。

        异常:
            KeyError: Task 在本进程内已被删除或不存在。
            ValueError: 重建结果不满足快照契约。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。

        副作用:
            先卸载该 task 的进程内快照副本，再读 canonical 三张表重建一份（终端会话列表取自
            进程内服务）；本方法不通知 subscriber（调用方需自行 ``publish_state`` 让客户端收敛）。

        并发:
            全程持 ``_lock``，与投影、订阅注册与删除清理互斥。
        """

        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            space.unload_snapshot()
            return self.get_state(task_id)

    def apply_planned(self, event: WorkflowEvent) -> TransportFrame:
        """将实时 projector 计划应用到进程本地状态并通知 subscriber。

        若不存在工作副本，canonical 冷读边界提供初始状态。planner 与变更在同一把锁下执行
        （与 subscriber 注册相同），因此首个 SSE 帧不会与排队的变更发生竞争。
        """
        task_id = event.task_id
        with self._lock:
            self._ensure_not_deleted(task_id)
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._rebuild(task_id))
            mutations = list(plan_event(event, state))
            for mutation in mutations:
                _apply_mutation(state, mutation)
            # 看板由工具 part 派生：只有带后台记录的事件会改变它（占位写入、并入、撤销、重启中断），
            # token 帧等其余事件不重算。
            if isinstance(event, ToolCallStatusChangedEvent) and event.background is not None:
                background = self._background_tasks(task_id, state)
                if background != state["background_tasks"]:
                    mutation = ConversationStateMutation("set", ("background_tasks",), background)
                    _apply_mutation(state, mutation)
                    mutations.append(mutation)
            # 仅追加的 token 帧不会改变快照形状或任何生命周期不变量。
            # ``_apply_mutation`` 中的 path/type 检查对这条热路径已足够；
            # 结构性变更仍付出完整校验代价。
            if _requires_full_snapshot_validation(mutations):
                validate_snapshot(state)
            change = TransportFrame(
                task_id=task_id,
                kind="mutation",
                mutations=tuple(mutations),
            )
            if mutations:
                self._publish(change)
            if isinstance(event, RunStatusChangedEvent):
                self._unload_if_unwatched(task_id)
            return change

    def _unload_if_unwatched(self, task_id: int) -> None:
        """没有订阅者且 Run 不在运行时卸载工作副本；须持 ``_lock`` 调用。

        工作副本只是投影缓存：没人看、也没有流式输出在往里写时留着它只占内存（每个打开过的长对话
        一份完整历史，进程生命周期内不释放）。下次读取或事件到达时按 canonical 记录重建。Run 还在
        运行时不卸载：流式正文没落库，重建会丢掉正在输出的内容。在最后一个订阅者离开、以及 Run
        状态变化（结束、挂起）这两个时刻判断。
        """

        if self._subscribers.get(task_id):
            return
        space = task_runtime_spaces.get(task_id)
        if space is None or not space.has_snapshot():
            return
        activity = space.get_working_snapshot(lambda: self._rebuild(task_id))["activity"]
        if activity is not None and activity["status"] == ConversationRunStatus.RUNNING.value:
            return
        space.unload_snapshot()
        log.info(
            "state_snapshot_unloaded",
            extra={
                "msg": "无人订阅且 Run 不在运行，卸载 Transport 快照工作副本",
                "data": {"task_id": task_id},
            },
        )

    def publish_state(
        self,
        task_id: int,
        state: ConversationStateSnapshot,
        mutations: tuple[ConversationStateMutation, ...] | None = None,
    ) -> TransportFrame:
        """发布一个其 canonical 数据库事务已经提交的状态。"""

        validate_snapshot(state)
        copied_state = copy.deepcopy(state)
        change = TransportFrame(
            task_id=task_id,
            kind="full",
            state=copied_state,
            mutations=mutations or (),
        )
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._publish(change)
        return change

    def subscribe_with_snapshot(
        self, task_id: int
    ) -> tuple[Subscriber, Callable[[], None], TransportFrame]:
        """在 ``_lock`` 下原子地挂载并准备首个 full 帧。"""

        loop = asyncio.get_running_loop()
        subscriber = Subscriber(loop)
        with self._lock:
            self._ensure_not_deleted(task_id)
            self._subscribers.setdefault(task_id, set()).add(subscriber)
            try:
                initial = self.get_state(task_id)
                first_frame = TransportFrame(
                    task_id=task_id,
                    kind="full",
                    state=initial,
                    mutations=(),
                )
            except Exception:
                self._subscribers[task_id].discard(subscriber)
                if not self._subscribers[task_id]:
                    self._subscribers.pop(task_id, None)
                raise

        def unsubscribe() -> None:
            """移除本 subscriber；重复调用安全。"""

            with self._lock:
                subscribers = self._subscribers.get(task_id)
                if subscribers is not None:
                    subscribers.discard(subscriber)
                    subscriber.close()
                    if not subscribers:
                        self._subscribers.pop(task_id, None)
                        if task_id not in self._deleted_task_ids:
                            self._unload_if_unwatched(task_id)

        return subscriber, unsubscribe, first_frame

    def mark_task_deleted(self, task_id: int) -> None:
        """canonical Task 删除后，丢弃进程本地状态与 subscriber。"""

        with self._lock:
            self._deleted_task_ids.add(task_id)
            subscribers = self._subscribers.pop(task_id, set())
            for subscriber in subscribers:
                with suppress(RuntimeError):
                    subscriber.loop.call_soon_threadsafe(subscriber.close)
            space = task_runtime_spaces.get(task_id)
            if space is not None:
                space.unload_snapshot()

    @classmethod
    def clear_process_state(cls) -> None:
        """丢弃所有易失的进程本地状态。

        进程生命周期钩子，非持久化操作。清空 subscriber 队列与已删除 task 的墓碑，使同一解释器中
        后续的存储重新初始化不会从上一次后端生命周期继承陈旧的副本。
        """

        with cls._lock:
            subscribers = [
                subscriber for group in cls._subscribers.values() for subscriber in group
            ]
            cls._subscribers.clear()
            cls._deleted_task_ids.clear()
        for subscriber in subscribers:
            with suppress(RuntimeError):
                subscriber.loop.call_soon_threadsafe(subscriber.close)
        task_runtime_spaces.close()

    def is_task_deleted(self, task_id: int) -> bool:
        """返回本进程是否已对该 Task 完成删除收尾。"""

        with self._lock:
            return task_id in self._deleted_task_ids

    def _ensure_not_deleted(self, task_id: int) -> None:
        """进程本地删除清理后，拒绝读取或投影。"""

        if task_id in self._deleted_task_ids:
            raise KeyError(task_id)

    def _rebuild(self, task_id: int) -> ConversationStateSnapshot:
        """读取 canonical 记录，并以结构化日志调用纯状态重建器。"""

        log.info(
            "state_rebuild_started",
            extra={
                "msg": "rebuilding conversation state from canonical records",
                "data": {"task_id": task_id},
            },
        )
        try:
            task = self._task_source.get(task_id)
            runs = self._run_source.list_by_task(task_id)
            context_rows = self._context_source.get(task_id, include_in_context=False)
            state = ConversationTaskStateRebuilder.rebuild(
                task,
                runs,
                context_rows,
            )
            state["queued_messages"] = self._queued_messages(task_id)
            state["terminal_sessions"] = self._terminal_sessions(task_id)
            state["background_tasks"] = self._background_tasks(task_id, state)
            state["foreground_waits"] = self._foreground_waits(task_id)
            state["handoffs"] = self._handoffs(task_id)
        except Exception as exc:
            log.exception(
                "state_rebuild_failed",
                extra={
                    "msg": "conversation state rebuild failed",
                    "data": {
                        "task_id": task_id,
                        "error_type": type(exc).__name__,
                        "error_code": getattr(exc, "code", None),
                    },
                },
            )
            raise
        log.info(
            "state_rebuild_completed",
            extra={
                "msg": "conversation state rebuilt from canonical records",
                "data": {
                    "task_id": task_id,
                    "message_count": len(state["messages"]),
                    "activity_run_id": (
                        state["activity"]["runId"] if state["activity"] is not None else None
                    ),
                },
            },
        )
        return state

    def _publish(self, change: TransportFrame) -> None:
        """存在 full 状态时安装该状态，并将帧入队给各 subscriber。"""

        if change.state is not None:
            task_runtime_spaces.get_or_create(change.task_id).replace_snapshot(change.state)
        subscribers = tuple(self._subscribers.get(change.task_id, set()))
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, change)
            except RuntimeError:
                log.warning(
                    "conversation_snapshot_subscriber_closed",
                    extra={
                        "msg": "snapshot subscriber loop closed",
                        "data": {"task_id": change.task_id},
                    },
                )


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """将 Transport 的 set/append-text 变更应用到内存状态。"""

    mutable_state = cast(dict[str, Any], state)
    if not mutation.path:
        if mutation.kind != "set" or not isinstance(mutation.value, dict):
            raise ValueError("root mutation must be set with an object")
        mutable_state.clear()
        mutable_state.update(copy.deepcopy(cast(dict[str, Any], mutation.value)))
        return
    parent: Any = mutable_state
    for key in mutation.path[:-1]:
        parent = parent[key]
    key = mutation.path[-1]
    if mutation.kind == "set":
        value = copy.deepcopy(mutation.value)
        if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
        return
    if mutation.kind != "append-text" or not isinstance(mutation.value, str):
        raise ValueError("append-text mutation must carry a string")
    current = parent[key]
    if not isinstance(current, str):
        raise TypeError("append-text target must be a string")
    parent[key] = current + mutation.value


def _requires_full_snapshot_validation(
    mutations: Sequence[ConversationStateMutation],
) -> bool:
    """返回一批变更是否会改变快照结构或生命周期形状。"""

    return any(
        mutation.kind != "append-text"
        or not mutation.path
        or not isinstance(mutation.value, str)
        or not mutation.value
        for mutation in mutations
    )


__all__ = ["ConversationTaskStateService"]
