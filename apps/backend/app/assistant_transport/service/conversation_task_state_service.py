"""进程内 Assistant Transport 状态与 canonical 冷读边界。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from threading import RLock
from typing import cast

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.snapshot_rebuild import rebuild_snapshot
from app.assistant_transport.projection.tool_presentation import presentation_catalog
from app.assistant_transport.projection.user_input_projection import build_presentation_parts
from app.assistant_transport.state.conversation_state_background_task import (
    ConversationBackgroundTask,
    project_background_tasks,
)
from app.assistant_transport.state.conversation_state_handoff import (
    ConversationHandoff,
    project_handoff,
)
from app.assistant_transport.state.conversation_state_message import ConversationQueuedMessage
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    validate_mutated,
    validate_snapshot,
)
from app.assistant_transport.state.conversation_state_terminal_session import (
    ConversationTerminalSession,
    to_terminal_session,
)
from app.assistant_transport.state.message_identity import user_message_id
from app.assistant_transport.state.snapshot_window import HistoryPage, history_page, window_snapshot
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.configuration import get_tool_registry
from app.config.logging.logger import log
from app.core.events import RunStatusChangedEvent, ToolCallStatusChangedEvent, WorkflowEvent
from app.core.workflow.message_queue.task_message import UserInputBody
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.task_runtime.task_runtime_space import TaskRuntimeSpace
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.tools.tool_handler.terminal.command_results import command_results

# 整表刷新的一个字段：从已物化的工作副本（看板要读其中的工具 part）算出最新值。
_FieldProducer = Callable[[ConversationStateSnapshot], object]


class ConversationTaskStateService:
    """持有进程本地 Transport 状态，并按需从 canonical 记录懒重建；进程级单例（``depends``）。

    工作副本是刻意易失的投影缓存，挂载在 Task 的 ``TaskRuntimeSpace`` 中：投影与订阅变化都不写数据库
    （展示数据不是事实源，子 Task 状态由应用级任务状态推送承载，不回写委派卡片）。首次读取时读出
    Task、它的全部 Run 与 context 行，交给 ``rebuild_snapshot`` 装配，再整表填入不在 context 里的
    事实：外部队列的排队消息、终端会话（``TerminalSessionService``）、看板与前台等待（后台结果登记表
    ``command_results``）以及进行中的交接（交接登记表）。之后复用这份工作副本，直到撤回重新发布、
    无人订阅时卸载或进程重置。

    契约守卫在写入侧：工作副本物化时整份校验一次，之后每次变更按触及范围校验（``validate_mutated``）；
    读取与发布只裁出历史窗口、只拷贝窗口，不再校验，所以读取成本与历史长度无关。

    并发：``_lock`` 是全部 space 快照工作副本的**唯一串行化点**（进程内一把锁）：懒重建、投影变更、
    发布、订阅注册与删除清理全部经过它，因此 space 自身不持有快照锁（见
    ``app.task_runtime.task_runtime_space``）。
    「Task 已删除」由 space 登记表在取 space 时拒绝（``KeyError``），这里不再另判。
    """

    def __init__(self) -> None:
        from app.service import depends

        self._task_source = depends.get_task_crud()
        self._run_source = depends.get_conversation_run_crud()
        self._context_source = depends.get_conversation_task_context_crud()
        self._lock = RLock()
        self._subscribers: dict[int, set[Subscriber]] = {}

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        """返回离开后端的快照：只带最近一段历史（``window_snapshot``），首次访问时懒重建。

        先裁窗口再深拷贝：完整历史留在工作副本里，拷贝与序列化只付窗口的代价。不在这里校验——
        工作副本由写入侧保证满足契约（见类说明）。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            ValueError: 首次物化的重建结果不满足快照契约。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。
        """

        with self._lock:
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._materialize(task_id))
            return copy.deepcopy(window_snapshot(state))

    def get_history_page(self, task_id: int, end: int) -> HistoryPage:
        """完整历史中位置 ``end`` 之前的一页；只拷贝这一页，不拷贝整份工作副本。

        位置与增量路径同一口径（完整历史里的下标），只在完整快照处才会变；``end`` 越界说明
        客户端的窗口来自已被替换的快照。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            ValueError: ``end`` 超出当前历史。
        """

        with self._lock:
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._materialize(task_id))
            return copy.deepcopy(history_page(state["messages"], end))

    # ── 进程内与队列事实的整表刷新 ──────────────────────────────────────────

    def refresh_queued_messages(self, task_id: int) -> None:
        """把排队投影整表同步为外部队列的现状。

        排队投影只镜像队列表：入队、撤回（删除、立即发送）之后都调这里，不区分增删。消费投影不从
        这里取内容（事件自带展示绑定），所以确认删除与消费投影之间刷掉这一条也无妨。读队列放在锁内：
        与消费投影串行，先读后提交的旧结果会被随后的消费投影覆盖，不会把已并入的消息留成排队。

        不全量重建：流式输出的 context 可能领先于尚未发出的 Transport delta，重建会提前展示正文
        再重复追加。这是命令提交之后、actor 推进之前的旁路：失败只记录，不能把已成功的命令报成
        500，也不能让 actor 跳过这次推进。
        """

        try:
            self._refresh(task_id, {"queued_messages": lambda _state: self._queued_messages(task_id)})
        except Exception as exc:
            log.exception(
                "queued_projection_refresh_failed",
                extra={
                    "msg": "排队投影刷新失败，照常推进；等后续刷新或重建收敛",
                    "data": {"task_id": task_id},
                },
                exc_info=exc,
            )

    def refresh_terminal_sessions(self, task_id: int) -> None:
        """会话变化通知入口：由 ``TerminalSessionRuntime.notify_changed`` 经终端副作用执行器调用
        （非 PTY 读线程、锁外），任意线程可调。条目在写入工作副本时按字段校验（见 ``_refresh``）。"""

        self._refresh(task_id, {"terminal_sessions": lambda _state: self._terminal_sessions(task_id)})

    def refresh_background_results(self, task_id: int) -> None:
        """后台结果变化入口：看板与前台等待各自有变化时一并发出（同一帧）；由登记表的通知线程调用。"""

        self._refresh(
            task_id,
            {
                "background_tasks": lambda state: self._background_tasks(task_id, state),
                "foreground_waits": lambda _state: command_results.foreground_calls(task_id),
            },
        )

    def refresh_handoffs(self, task_id: int) -> None:
        """交接变化入口：由交接登记表的通知线程调用。"""

        self._refresh(task_id, {"handoffs": lambda _state: self._handoffs(task_id)})

    def _refresh(self, task_id: int, producers: Mapping[str, _FieldProducer]) -> None:
        """整表重读这几个顶层字段，有变化的各发一条 ``set`` mutation（同一帧）。

        只更新已物化的快照：未物化时之后的懒重建自会读到最新值；Task 已删除或 space 不存在时静默
        返回——删除收尾（关会话、撤后台工作）也会触发通知，此时已无人订阅。不用 ``get_or_create``，
        否则一次迟到的通知会为已删除或从未打开的 Task 物化 space 与 actor。读事实放在锁内，并发
        通知按序落定，不会以旧值覆盖新值。
        """

        with self._lock:
            if task_runtime_spaces.is_deleted(task_id):
                return
            space = task_runtime_spaces.get(task_id)
            if space is None or not space.has_snapshot():
                return
            state = space.get_working_snapshot(lambda: self._materialize(task_id))
            snapshot = cast(dict[str, object], state)
            mutations = tuple(
                ConversationStateMutation("set", (key,), value)
                for key, produce in producers.items()
                if (value := produce(state)) != snapshot[key]
            )
            if not mutations:
                return
            for mutation in mutations:
                mutation.apply_to(state)
            self._validate_or_discard(space, state, mutations)
            self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=mutations))

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
            state["messages"], command_results.live_background(task_id)
        )

    @staticmethod
    def _terminal_sessions(task_id: int) -> list[ConversationTerminalSession]:
        from app.service.depends import get_terminal_session_service

        runtimes = get_terminal_session_service().task_runtimes(task_id)
        return [to_terminal_session(runtime.snapshot()) for runtime in runtimes]

    # ── 实时投影、发布与订阅 ────────────────────────────────────────────────

    def apply_planned(self, event: WorkflowEvent) -> TransportFrame:
        """将实时投影计划应用到工作副本并通知订阅者。

        没有工作副本时由 canonical 冷读提供初始状态。规划与变更在同一把锁下执行（与订阅注册相同），
        因此首个 SSE 帧不会与排队的变更交错。

        异常:
            KeyError: Task 不存在或在本进程内已被删除；事件引用的消息、part 或调用不存在。
            ValueError: 事件不满足投影规则，或结构性变更后快照不满足契约。
        """

        task_id = event.task_id
        with self._lock:
            space = task_runtime_spaces.get_or_create(task_id)
            state = space.get_working_snapshot(lambda: self._materialize(task_id))
            mutations = list(plan_event(event, state, presentation_catalog(get_tool_registry())))
            for mutation in mutations:
                mutation.apply_to(state)
            # 看板由工具 part 派生：只有带后台记录的事件会改变它（占位写入、并入、撤销、重启中断），
            # token 帧等其余事件不重算。
            if isinstance(event, ToolCallStatusChangedEvent) and event.background is not None:
                background = self._background_tasks(task_id, state)
                if background != state["background_tasks"]:
                    mutation = ConversationStateMutation("set", ("background_tasks",), background)
                    mutation.apply_to(state)
                    mutations.append(mutation)
            self._validate_or_discard(space, state, mutations)
            change = TransportFrame(task_id=task_id, kind="mutation", mutations=tuple(mutations))
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
        activity = space.get_working_snapshot(lambda: self._materialize(task_id))["activity"]
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

    def republish_from_canonical(self, task_id: int) -> None:
        """丢弃工作副本、按 canonical 重建并向订阅者发布整份快照。

        唯一的调用方是撤回：撤回改了库里的上下文，副本必须按 canonical 重来；撤回只在 Task 静止时
        进行，没有需要保留的瞬时模型活动。重建与发布在同一临界区：分两次取锁时，中间到达的投影会被
        这份更旧的 full 帧覆盖，客户端随即丢掉那条已经发出去的增量。

        异常:
            KeyError: Task 不存在或在本进程内已被删除。
            ValueError: 重建结果不满足快照契约。
            sqlalchemy.exc.SQLAlchemyError: canonical 数据源读取失败。
        """

        with self._lock:
            space = task_runtime_spaces.get_or_create(task_id)
            space.unload_snapshot()
            state = space.get_working_snapshot(lambda: self._materialize(task_id))
            self._publish(
                TransportFrame(
                    task_id=task_id, kind="full", state=copy.deepcopy(window_snapshot(state))
                )
            )

    def subscribe_with_snapshot(
        self, task_id: int
    ) -> tuple[Subscriber, Callable[[], None], TransportFrame]:
        """在锁内原子地挂上订阅者并准备首个 full 帧。

        异常:
            KeyError: Task 不存在或在本进程内已被删除（订阅随即撤下）。
        """

        loop = asyncio.get_running_loop()
        subscriber = Subscriber(loop)
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(subscriber)
            try:
                first_frame = TransportFrame(
                    task_id=task_id, kind="full", state=self.get_state(task_id)
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
                        if not task_runtime_spaces.is_deleted(task_id):
                            self._unload_if_unwatched(task_id)

        return subscriber, unsubscribe, first_frame

    def release_deleted_task(self, task_id: int) -> None:
        """Task 删除提交后关闭它的订阅、丢弃快照工作副本。

        调用方先在 ``task_runtime_spaces.mark_deleted`` 记下删除：订阅与读取取 space 时就会被拒绝，先记
        删除再释放，就不会有新订阅在释放之后挂上来。
        """

        with self._lock:
            subscribers = self._subscribers.pop(task_id, set())
            for subscriber in subscribers:
                with suppress(RuntimeError):
                    subscriber.loop.call_soon_threadsafe(subscriber.close)
            space = task_runtime_spaces.get(task_id)
            if space is not None:
                space.unload_snapshot()

    def close_subscribers(self) -> None:
        """进程生命周期钩子：关闭全部订阅（``depends.reset_service_dependencies`` 在丢弃单例前调用）。"""

        with self._lock:
            subscribers = [
                subscriber for group in self._subscribers.values() for subscriber in group
            ]
            self._subscribers.clear()
        for subscriber in subscribers:
            with suppress(RuntimeError):
                subscriber.loop.call_soon_threadsafe(subscriber.close)

    @staticmethod
    def _validate_or_discard(
        space: TaskRuntimeSpace,
        state: ConversationStateSnapshot,
        mutations: Sequence[ConversationStateMutation],
    ) -> None:
        """变更已写进工作副本后的契约检查；须持 ``_lock`` 调用。

        违约时丢弃工作副本再抛出：读取不再校验，留着它会把违约内容发给之后的读取方；丢弃后下次
        读取或事件到达时按 canonical 重建（重建结果在物化时整份校验）。违约的变更不发布。
        """

        try:
            validate_mutated(state, mutations)
        except ValueError:
            space.unload_snapshot()
            raise

    def _materialize(self, task_id: int) -> ConversationStateSnapshot:
        """工作副本的加载函数：重建并整份校验一次；之后由 ``validate_mutated`` 守住每次变更。"""

        state = self._rebuild(task_id)
        validate_snapshot(state)
        return state

    def _rebuild(self, task_id: int) -> ConversationStateSnapshot:
        """读取 canonical 记录与进程内事实，装配完整快照（带结构化日志）。"""

        log.info(
            "state_rebuild_started",
            extra={
                "msg": "rebuilding conversation state from canonical records",
                "data": {"task_id": task_id},
            },
        )
        try:
            state = rebuild_snapshot(
                self._task_source.get(task_id),
                self._run_source.list_by_task(task_id),
                self._context_source.get(task_id, include_in_context=False),
                presentation_catalog(get_tool_registry()),
            )
            state["queued_messages"] = self._queued_messages(task_id)
            state["terminal_sessions"] = self._terminal_sessions(task_id)
            state["background_tasks"] = self._background_tasks(task_id, state)
            state["foreground_waits"] = command_results.foreground_calls(task_id)
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
                    "activity_turn": (
                        state["activity"]["turn"] if state["activity"] is not None else None
                    ),
                },
            },
        )
        return state

    def _publish(self, change: TransportFrame) -> None:
        """把帧交给各订阅者；须持 ``_lock`` 调用。full 帧的 ``state`` 是已裁好的窗口副本，工作副本不经它改变。"""

        for subscriber in tuple(self._subscribers.get(change.task_id, set())):
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


__all__ = ["ConversationTaskStateService"]
