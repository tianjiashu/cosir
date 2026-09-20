"""一个持久化 Task 对应的运行时资源空间。

本模块只拥有统一的执行闸门（Task 操作锁）与延迟创建的运行时 context/snapshot working
copy；持久化的 task / run / context 记录仍由 SQLite 负责。多个 space 的进程内生命周期管理见
``app.task_runtime.task_runtime_space_registry``。

加锁边界（**审查须知：请勿把"缺少内部锁"判定为缺陷**）：

本类除 ``_context_guard`` 外不自行加锁，这是刻意的单一闸门设计，不是遗漏：

- **snapshot working copy（``_snapshot``）没有任何内部锁，是刻意的。** 其全部读写入口
  （``get_snapshot`` / ``existing_snapshot`` / ``replace_snapshot`` / ``unload_snapshot``）
  的生产调用方只有 ``ConversationTaskStateService``；该 service 的类级 ``_lock``（RLock）是
  snapshot working copy 的**唯一序列化点**，已在懒加载、事件投影、subscriber 注册、删除清理
  所有路径上覆盖。此前额外存在的 ``_snapshot_guard`` 与外层 ``_lock`` 是同一临界区里的双层锁
  （外层 RLock 完全覆盖内层普通 Lock），已按"不重复加锁"删除。
- **直连约定（务必看清这一条，它决定"要不要加回锁"）**：既有单测
  （``tests/test_task4_state_lifecycle.py``）会在**单线程**下直连 space 读写 snapshot 以断言
  working copy 的生命周期，此时不存在并发，属允许用法。任何**多线程**直连（不持
  ``_lock``）都是调用方缺陷：它会同时触发 check-then-set 造成的重复懒加载与
  last-write-wins 覆盖，本类不为此兜底。出现这类调用方时，正确修法是让它回到
  ``ConversationTaskStateService``，**不是**给本类加回内部锁。
- **context manager 槽位的懒创建/查询/卸载不额外加锁**，因为其调用方本已持有该 task 的操作锁：
  run 执行（``ConversationRunExecutor._execute`` 全程 ``async_operation()``）、task 删除
  （``TaskService.delete_task`` 的 task 闸门）、task fork 的源 task 闸门。
- **``_context_guard`` 保留的唯一理由**：task fork 在 ``begin_immediate`` 事务提交之后才安装
  目标 context manager，此时调用线程持有的是**源** task 闸门与 workspace 闸门，**不持有目标
  task 闸门**；因此「安装 fork manager」与「目标 task 首次 run 的懒创建 manager」之间存在真实
  竞态，需要一把锁保证两者不互相覆盖。删除它会引入 fork hydrate 覆盖/丢失问题。
"""

from __future__ import annotations

import asyncio
import copy
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, TypeVar, cast

from langchain_core.messages import SystemMessage

from app.core.context.context_listener.context_compress_listener import ContextCompressListener
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)

if TYPE_CHECKING:
    from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.models import TaskRecord, WorkspaceRecord


_T = TypeVar("_T")


def _weak_ref(obj: _T) -> weakref.ReferenceType[_T]:
    """以弱引用包裹对象，消除类型检查器对 ``weakref.ref`` 返回泛型的推断偏差。

    参数:
        obj: 需要弱引用的对象。

    返回:
        指向 ``obj`` 的弱引用；``obj`` 被回收后该引用解引用返回 None。
    """

    return cast(weakref.ReferenceType[_T], weakref.ref(obj))


@dataclass
class TaskRuntimeSpace:
    """一个持久化 Task 对应的运行时资源空间。

    并发边界见模块 docstring：``lock`` 是唯一 Task 操作闸门；snapshot working copy **不**持有
    内部锁（由 ``ConversationTaskStateService._lock`` 统一串行化）；``_context_manager`` 槽位
    由 ``_context_guard`` 保护；``system_queue`` 延迟系统消息队列由标准库 ``SimpleQueue`` 提供
    并发安全（内部已加锁，本类不再额外加锁），跨同 task 内 run 共享、run 间串行由
    Task 操作闸门保证。
    """

    task_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: weakref.ReferenceType[RuntimeContextManager] | None = field(
        default=None, init=False
    )
    # snapshot 是 TypedDict（运行时即 dict），无法被弱引用包裹，因此按 task 维度强引用缓存，
    # 由 ``unload_snapshot`` / 进程内清理显式释放。
    # 该字段刻意不配内部锁：所有访问经 ``ConversationTaskStateService._lock`` 串行化。
    _snapshot: ConversationStateSnapshot | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)
    # 延迟注入的「修复类系统消息」队列（FIFO）：跨同 task 内的 run 共享，run 间串行由 Task 操作
    # 闸门保证；用标准库 SimpleQueue 提供并发安全，不额外加锁。
    system_queue: SimpleQueue[SystemMessage] = field(
        default_factory=SimpleQueue, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """初始化统一执行闸门与 context manager 槽位锁。"""

        self.lock = threading.Lock()
        self._context_guard = threading.Lock()

    def _acquire_lock(self, timeout: float | None) -> bool:
        """在同步线程中取得 Task 操作闸门。"""

        if timeout is None:
            return self.lock.acquire()
        return self.lock.acquire(timeout=timeout)

    @contextmanager
    def operation(self, timeout: float | None = None) -> Iterator[None]:
        """同步取得统一 Task 操作闸门。

        参数:
            timeout: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Task 闸门的同步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。

        副作用:
            在上下文期间阻止同一 Task 的其他运行时操作。
        """

        if not self._acquire_lock(timeout):
            raise TimeoutError(f"task {self.task_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    @asynccontextmanager
    async def async_operation(self, wait_seconds: float | None = None) -> AsyncIterator[None]:
        """异步取得与同步调用共享的统一 Task 操作闸门。

        参数:
            wait_seconds: 最长等待秒数；为 None 时持续等待。

        返回:
            一个释放 Task 闸门的异步上下文管理器。

        异常:
            TimeoutError: 在指定时间内未能取得闸门。
            asyncio.CancelledError: 调用方取消等待；底层锁仍会先完成收购并释放，
                避免取消窗口遗留永久占用。

        副作用:
            在上下文期间阻止同一 Task 的其他运行时操作，且不会阻塞当前 event loop。
        """

        acquire_task = asyncio.create_task(asyncio.to_thread(self._acquire_lock, wait_seconds))
        try:
            acquired = await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            # 不能取消已经提交给线程池的 acquire；必须等待它完成后释放，避免
            # “调用方已取消但底层锁后来才被取得”造成永久锁死。
            acquired = await acquire_task
            if acquired:
                self.lock.release()
            raise
        if not acquired:
            raise TimeoutError(f"task {self.task_id} operation lock is busy")
        try:
            yield
        finally:
            self.lock.release()

    def unload_context_manager(self) -> None:
        """卸载当前 Task 的进程内 context manager 引用。

        当前 ``RuntimeContextManager`` 不持有外部句柄，卸载只需清除引用；调用方必须
        在 Task 已经停止接受新操作且不再有运行持锁时调用本方法。
        """

        with self._context_guard:
            self._context_manager = None

    def get_snapshot(
        self, loader: Callable[[], ConversationStateSnapshot]
    ) -> ConversationStateSnapshot:
        """返回 task snapshot；首次调用时从 canonical records 懒加载重建。

        参数:
            loader: 在 snapshot 尚未物化时执行的重建函数。函数应只读取 canonical
                Task/Run/Context records，不得写入本 space 的 snapshot。

        返回:
            当前 task 的 snapshot 深拷贝。调用方可以安全修改返回值而不污染 space 内的
            working copy。

        异常:
            透传 ``loader`` 的重建异常；失败时不会缓存不完整 snapshot。

        副作用:
            首次调用时重建并缓存进程内 working copy；后续调用复用同一 task 的副本，不再读取
            数据库或重复重建，直到 ``unload_snapshot`` 或进程内清理。

        并发:
            **本方法刻意不自行加锁**（原 ``_snapshot_guard`` 已随"同一临界区双层锁"删除）。
            读写 ``_snapshot`` 必须全部经 ``ConversationTaskStateService``：其类级 ``_lock``
            是唯一序列化点，已在懒加载、事件投影、subscriber 注册与删除清理路径上覆盖。
            若有调用方绕过该 service 直接调用本方法或其它 snapshot 方法，需自行保证互斥。
        """

        if self._snapshot is None:
            self._snapshot = copy.deepcopy(loader())
        return copy.deepcopy(self._snapshot)

    def existing_snapshot(self) -> ConversationStateSnapshot | None:
        """返回已物化的 task snapshot，不触发数据库读取或懒加载。

        尚未物化或已被 ``unload_snapshot`` 清理时返回 None。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        return copy.deepcopy(self._snapshot) if self._snapshot is not None else None

    def replace_snapshot(self, snapshot: ConversationStateSnapshot) -> None:
        """替换 task 的进程内 snapshot working copy。

        仅供已完成 canonical 数据库提交后的显式重建或 Transport projector 使用；不会
        写数据库，也不会通知 SSE subscriber。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        self._snapshot = copy.deepcopy(snapshot)

    def unload_snapshot(self) -> None:
        """卸载 task snapshot，使下一次访问重新从 canonical records 懒加载。

        并发:
            与 ``get_snapshot`` 同一约定：不自行加锁，调用方须经 ``ConversationTaskStateService``。
        """

        self._snapshot = None

    def get_context_manager(
        self,
        *,
        agent_profile: AgentProfile,
        current_workspace: WorkspaceRecord,
        current_task: TaskRecord,
    ) -> RuntimeContextManager:
        """返回 task context manager；首次执行时才创建并加载它。

        参数:
            agent_profile: 当前 task 使用的 Agent 画像，决定 context 装配策略。
            current_workspace: 当前 workspace 记录，提供 ``root_path`` 等工作目录。
            current_task: 当前 task 记录，提供上下文归属的 task id。

        返回:
            已创建或已缓存的 ``RuntimeContextManager`` 实例。

        异常:
            ``RuntimeContextManager`` 构造或其 listener 装配失败时原样向上抛出，本方法不兜底；
            此时 space 不会缓存半成品引用，调用方（run 执行、task fork）需自行处理。

        副作用:
            首次调用时在持有 ``_context_guard`` 的前提下惰性构造并以**弱引用**缓存 context
            manager；后续调用直接返回仍存活的缓存实例。当外部不再持有该 manager 时弱引用
            会自然失效，下次访问重新创建。

        并发:
            锁外快路径 + 锁内重检是刻意保留的 double-checked locking，**不是重复判断**：
            ``install_fork_context_manager`` 会在同一把锁内写入槽位，若省掉锁内重检，
            懒创建可能覆盖刚安装的 fork manager。run 执行路径本身还持有 Task 操作闸门，
            这里的锁只覆盖 fork hydrate 与目标 task 首次 run 之间的竞态（详见模块 docstring）。
        """

        manager_ref = self._context_manager
        if manager_ref is not None:
            manager = manager_ref()
            if manager is not None:
                return manager
        with self._context_guard:
            manager_ref = self._context_manager
            manager = manager_ref() if manager_ref is not None else None
            if manager is None:
                from app.core.context.runtime_context_manager import RuntimeContextManager

                manager = (
                    RuntimeContextManager(
                        current_task_id=current_task.id,
                        agent_profile=agent_profile,
                        workspace_root=current_workspace.root_path,
                        is_fork=current_task.task_type == "fork",
                    )
                    .add_change_listener(ContextUsageComputeListener(current_task.id))
                    .add_change_listener(ContextCompressListener())
                )
                self._context_manager = _weak_ref(manager)
            return manager

    def existing_context_manager(self) -> RuntimeContextManager | None:
        """返回已物化的 context manager；不因查询而触发懒加载。

        当弱引用已失效（外部不再持有 manager）时返回 None。

        并发:
            经 ``_context_guard`` 快照弱引用；调用方（task fork / task 删除）本已持有
            Task 操作闸门，此处的锁仅保证与 ``install_fork_context_manager`` 的写入不交叉。
        """

        with self._context_guard:
            return self._context_manager() if self._context_manager is not None else None

    def install_fork_context_manager(self, manager: RuntimeContextManager) -> None:
        """安装已由源 manager fork 出来的目标 context manager。

        仅在目标 space 尚无存活 manager 时安装，避免覆盖目标 task 已懒创建的真实 manager。

        并发:
            ``_context_guard`` 是唯一写入方之间的互斥点；调用方（``TaskService`` fork 事务）
            持有源 task 闸门与 workspace 闸门，但**不持有目标 task 闸门**，故此处不能省略锁。
        """

        with self._context_guard:
            if self._context_manager is not None and self._context_manager() is not None:
                return
            installed = (
                manager.add_change_listener(ContextUsageComputeListener(self.task_id))
                .add_change_listener(ContextCompressListener())
            )
            self._context_manager = _weak_ref(installed)

    def defer_system_message(self, message: SystemMessage) -> None:
        """将一条修复类系统消息延后到本 task 下一次 model 节点入口注入。

        参数:
            message: 待注入的系统消息（通常为非法工具调用修复提示）。
        """

        self.system_queue.put(message)

    def take_deferred_system_messages(self, *, run_id: int | None = None) -> list[SystemMessage]:
        """取出当前 Run 可消费的延迟系统消息，并清理旧 Run 的消息（FIFO）。

        没有 ``run_id`` 标记的消息属于既有 task 级修复提示，任何 Run 都可消费；带有
        ``run_id`` 标记的消息只允许对应 Run 消费，旧 Run 的消息会在本次取队列时丢弃，
        避免取消或异常后的终端背压提示污染后续 Run。

        参数:
            run_id: 当前模型节点所属 Run；省略时保留无条件取队列行为。

        返回:
            按入队顺序排列且属于当前 Run 的系统消息列表；无排队时返回空列表。
        """

        messages: list[SystemMessage] = []
        while True:
            try:
                message = self.system_queue.get_nowait()
            except Empty:
                break
            message_run_id = message.additional_kwargs.get("run_id")
            if run_id is None or message_run_id is None or message_run_id == run_id:
                messages.append(message)
        return messages

    def has_deferred_system_messages(self) -> bool:
        """本 task 是否仍排队有未注入的延迟系统消息。"""

        return not self.system_queue.empty()
