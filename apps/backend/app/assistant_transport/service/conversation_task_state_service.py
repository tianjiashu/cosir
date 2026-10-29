"""进程内 Assistant Transport 状态与 canonical 冷读边界。"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from contextlib import suppress
from threading import RLock
from typing import Any, ClassVar, cast

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.snapshot_navigation import iter_parts
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_background_task import (
    ConversationBackgroundTask,
    project_background_task,
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
from app.assistant_transport.stream import TransportFrame
from app.assistant_transport.stream.subscriber import Subscriber
from app.config.logging.logger import log
from app.core.events import WorkflowEvent
from app.core.workflow.message_queue.task_message import UserInputBody
from app.models.message_metadata import ToolMessageMetadata
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces



class ConversationTaskStateService:
    """持有进程本地 Transport 状态，并按需从 canonical 记录懒重建。

    工作副本是刻意易失的，挂载在 task 的 ``TaskRuntimeSpace`` 中：普通 projector 变更与 subscriber
    变化都不写数据库行；child Run 状态投影只会更新已有工具行的 Transport metadata。某个 task 的
    首次读取会加载一个 Task、其全部 Run 与全部 context 行，
    再把纯 wire 状态构造委托给 ``ConversationTaskStateRebuilder``；排队消息与终端会话列表不在
    canonical context 里，重建后分别从外部队列与 ``TerminalSessionService`` 整表填入。之后的读取
    复用该 task 级的进程内内存快照，直到显式 rebuild 或进程重置。

    参数:
        task_source: Task 的 CRUD 类数据源；默认取进程依赖。
        run_source: Run 的 CRUD 类数据源；默认取进程依赖。
        context_source: Context 的 CRUD 类数据源；默认取进程依赖。

    副作用:
        ``get_state`` 可能读取三张 canonical SQLite 表，并只缓存一份内存副本。
            ``apply_planned`` 与 ``publish_state`` 更新进程本地状态并通知 SSE subscriber；child
            状态投影还会尽力持久化已有工具的 UI metadata。
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

    def refresh_queued_messages(self, task_id: int) -> None:
        """把外部队列里待处理的用户输入按身份新增或替换进排队投影，只发这一处 mutation。

        调用时机：actor 推进（投递 / 开 Run）之前，以及排队消息原地编辑之后。消费投影
        （``plan_user_input_consumed``）直接搬移排队条目的 parts、不重建，所以消息进 mailbox 之前
        排队投影里必须已有它的最新内容。

        只增改、不删除：条目的移除只归消费投影。确认删除与消费投影之间有窗口，这里若按队列整表
        覆盖，会在窗口内删掉刚被确认的条目，随后消费投影找不到它。快照全量重建时照常整表写入。

        不全量重建：流式输出的 context 可能领先于尚未发出的 Transport delta，重建会提前展示正文
        再重复追加。snapshot 尚未物化时什么也不做，之后的懒重建自会读到队列。读队列放在锁内，
        让并发刷新按序落定，不会以旧内容覆盖新内容。
        """
        try:
            with self._lock:
                self._ensure_not_deleted(task_id)
                space = task_runtime_spaces.get_or_create(task_id)
                if not space.has_snapshot():
                    return
                state = space.get_working_snapshot(lambda: self._rebuild(task_id))
                current = state.get("queued_messages", [])
                merged = _upsert_queued(current, self._queued_messages(task_id))
                if merged == current:
                    return
                mutation = ConversationStateMutation("set", ("queued_messages",), merged)
                _apply_mutation(state, mutation)
                self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=(mutation,)))
        except Exception as exc:
            log.exception(
                "queued_projection_refresh_failed",
                extra={
                    "msg": "排队投影刷新失败，照常推进；等后续刷新或重建收敛",
                    "data": {"task_id": self.task_id},
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
                parts=ConversationTaskStateRebuilder.build_user_parts(item.body.presentation),
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

    def refresh_deferred_results(self, task_id: int) -> None:
        """延迟结果变化入口：整表重读看板与前台等待，各自有变化时发 ``set`` mutation（同一帧）。

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
                    ("background_tasks", self._background_tasks(task_id)),
                    ("foreground_waits", self._foreground_waits(task_id)),
                )
                if value != state[key]
            )
            if not mutations:
                return
            for mutation in mutations:
                _apply_mutation(state, mutation)
            self._publish(TransportFrame(task_id=task_id, kind="mutation", mutations=mutations))

    @staticmethod
    def _background_tasks(task_id: int) -> list[ConversationBackgroundTask]:
        from app.core.tool.background.deferred_registry import deferred_registry

        return [project_background_task(view) for view in deferred_registry.entries(task_id)]

    @staticmethod
    def _foreground_waits(task_id: int) -> list[str]:
        from app.core.tool.background.deferred_registry import deferred_registry

        return deferred_registry.foreground_calls(task_id)

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
            # A child can finish before the parent tool 消息 is persisted. Reconcile any
            # already-present child display payload after every event so that this race does
            # not leave the newly materialized parent card at ``running`` forever.
            mutations.extend(
                self._reconcile_child_delegation_statuses(
                    state,
                    persist=True,
                    parent_task_id=task_id,
                )
            )
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
            return change

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
            state["background_tasks"] = self._background_tasks(task_id)
            state["foreground_waits"] = self._foreground_waits(task_id)
            self._reconcile_child_delegation_statuses(state, persist=False)
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

    def refresh_parent_delegation(
        self,
        child_task_id: int,
        run_id: int,
        status: str,
    ) -> TransportFrame | None:
        """把子 Task 最新 Run 的 canonical 状态投影到已物化的父任务委派卡片。

        子 Run 的状态事件先更新子任务自身 snapshot，再调用此方法同步父任务中对应的
        动态 child display。卡片按 ``child_task_id`` 定位、展示子 Task 当前（最新 Run）的
        状态，不跟踪某一条 Run：同一子任务的委派卡片与追加消息卡片一起收敛。``run_id`` 是
        事件所属 Run，不是子 Task 最新 Run 时（旧 Run 的迟到事件）忽略。只处理已经存在的
        ``delegation-result`` 与 ``child-agent-result(operation=send)``；状态查询和等待结果
        是历史快照，不会被异步改写。父任务未物化时仍会先持久化 metadata，后续冷读可以恢复。
        """

        with self._lock:
            child_task = self._task_source.get(child_task_id)
            parent_task_id = child_task.parent_task_id
            if parent_task_id is None or child_task.current_run_id != run_id:
                return None
            # 卡片跟随子 Run 的全部生命周期状态，含 suspended（子 Agent 在等审批或消息）。
            child_run = self._run_source.get(run_id)
            if child_run.status != status:
                log.debug(
                    "parent_delegation_stale_status_event",
                    extra={
                        "msg": "忽略与 canonical child Run 状态不一致的委派状态事件",
                        "data": {
                            "child_task_id": child_task_id,
                            "run_id": run_id,
                            "event_status": status,
                            "canonical_status": child_run.status,
                        },
                    },
                )
            status = child_run.status
            self._persist_child_display_status(parent_task_id, child_task_id, status)
            parent_space = task_runtime_spaces.get(parent_task_id)
            if parent_space is None:
                return None
            state = parent_space.existing_snapshot()
            if state is None:
                return None

            mutations: list[ConversationStateMutation] = []
            for path, part in iter_parts(state):
                if not isinstance(part, dict) or part.get("type") != "tool-call":
                    continue
                display = part.get("display_data")
                if not _matches_dynamic_child_display(display, child_task_id):
                    continue
                updated_display = dict(display)
                updated_display["status"] = status
                mutations.append(
                    ConversationStateMutation("set", (*path, "display_data"), updated_display)
                )
            if not mutations:
                return None
            for mutation in mutations:
                _apply_mutation(state, mutation)
            return self.publish_state(parent_task_id, state, tuple(mutations))

    def _reconcile_child_delegation_statuses(
        self,
        state: ConversationStateSnapshot,
        *,
        persist: bool,
        parent_task_id: int | None = None,
    ) -> list[ConversationStateMutation]:
        """按已有 child display locator 修正其缓存的 canonical Run 状态。

        该修复只处理 snapshot 中已经存在的动态 child display，不会从工具名、Task 关系或
        缺失的 ``display_data`` 创建任何 UI part。冷重建因此仍保持 display_data 优先，
        但能收敛「子 Run 终态事件早于父 tool 消息 持久化」留下的窗口。

        参数:
            state: 当前 task 的 Transport working copy。
            persist: 是否把发生变化的 metadata 同步回父任务 context；冷重建传 False，
                事件投影传 True。
            parent_task_id: 事件投影时用于持久化 metadata 的父任务标识；冷重建无需提供。

        返回:
            已应用到 state 的 display_data set mutations。

        副作用:
            ``persist=True`` 时更新父任务 context 的 Transport metadata；持久化失败只记
            日志，不阻断已经完成的内存 Transport 投影。
        """

        if persist and parent_task_id is None:
            raise ValueError("parent_task_id is required when persisting child status")

        child_task_ids: set[int] = set()
        for _, part in iter_parts(state):
            if not isinstance(part, dict):
                continue
            display = part.get("display_data")
            if not _is_dynamic_child_display(display):
                continue
            child_task_id = cast(dict[str, object], display).get("child_task_id")
            if _is_positive_int(child_task_id):
                child_task_ids.add(cast(int, child_task_id))

        # 卡片展示子 Task 最新 Run 的状态：按 Task 的当前 Run 指针取，不跟踪委派时的那条 Run。
        statuses_by_task: dict[int, str] = {}
        for child_task_id in child_task_ids:
            try:
                current_run_id = self._task_source.get(child_task_id).current_run_id
            except KeyError:
                # 子任务可被单独删除，父任务的卡片保留最后一次投影的状态。
                continue
            if current_run_id is None:
                continue
            statuses_by_task[child_task_id] = self._run_source.get(current_run_id).status

        mutations: list[ConversationStateMutation] = []
        changed: dict[int, str] = {}
        for path, part in list(iter_parts(state)):
            if not isinstance(part, dict):
                continue
            display = part.get("display_data")
            if not isinstance(display, dict) or not _is_dynamic_child_display(display):
                continue
            child_task_id = display.get("child_task_id")
            if not _is_positive_int(child_task_id):
                continue
            status = statuses_by_task.get(child_task_id)
            if status is None or display.get("status") == status:
                continue
            updated_display = dict(display)
            updated_display["status"] = status
            mutation = ConversationStateMutation("set", (*path, "display_data"), updated_display)
            _apply_mutation(state, mutation)
            mutations.append(mutation)
            changed[child_task_id] = status
        if persist and parent_task_id is not None:
            # 持久化按子 Task 一次写完它的全部卡片，同一子任务的多张卡片不重复写。
            for child_task_id, status in changed.items():
                self._persist_child_display_status(parent_task_id, child_task_id, status)
        return mutations

    def _persist_child_display_status(
        self,
        parent_task_id: int,
        child_task_id: int,
        status: str,
    ) -> None:
        """持久化父任务中指向该子 Task 的动态 child display 状态。"""

        try:
            rows = self._context_source.get(parent_task_id, include_in_context=False)
            for row in rows:
                # 只为前端卡片保持最新状态；已移出投影的行（编辑替换掉的历史）保持冻结。
                if not row.project_to_transport:
                    continue
                if not isinstance(row.metadata, ToolMessageMetadata):
                    continue
                display = row.metadata.display_data
                if not isinstance(display, dict) or not _matches_dynamic_child_display(
                    display, child_task_id
                ):
                    continue
                self._context_source.update_child_display_status(
                    parent_task_id, row.sequence, child_task_id, status
                )
        except Exception:
            log.exception(
                "parent_delegation_display_persist_failed",
                extra={
                    "msg": "子 Run 状态已投影到内存，但父委派 display metadata 持久化失败",
                    "data": {
                        "parent_task_id": parent_task_id,
                        "child_task_id": child_task_id,
                        "status": status,
                    },
                },
            )

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


def _is_positive_int(value: object) -> bool:
    """Return whether a child locator is a real positive integer."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_dynamic_child_display(value: object) -> bool:
    """Return whether a display payload represents a live child Task reference."""

    if not isinstance(value, dict):
        return False
    if value.get("kind") == "delegation-result":
        return True
    return value.get("kind") == "child-agent-result" and value.get("operation") == "send"


def _matches_dynamic_child_display(value: object, child_task_id: int) -> bool:
    """Match a live child display by its child Task locator."""

    if not _is_dynamic_child_display(value) or not isinstance(value, dict):
        return False
    return value.get("child_task_id") == child_task_id


def _upsert_queued(
        current: list[ConversationQueuedMessage],
        pending: list[ConversationQueuedMessage],
) -> list[ConversationQueuedMessage]:
    """已有条目按身份原位替换，新条目按队列顺序追加；不在 ``pending`` 里的已有条目保留。"""

    latest = {message["id"]: message for message in pending}
    merged = [latest.pop(message["id"], message) for message in current]
    merged.extend(message for message in pending if message["id"] in latest)
    return merged


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
    mutations: tuple[ConversationStateMutation, ...],
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
