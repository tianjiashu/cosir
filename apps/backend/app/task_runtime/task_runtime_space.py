"""一个持久化 Task 对应的运行时资源空间。

本模块只拥有统一的执行闸门（Task 操作锁）与延迟创建的运行时 context/snapshot working
copy；持久化的 task / run / context 记录仍由 SQLite 负责。多个 space 的进程内生命周期管理见
``app.task_runtime.task_runtime_space_registry``。
"""

from __future__ import annotations

import asyncio
import copy
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar, cast

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
    """一个持久化 Task 对应的运行时资源空间。"""

    task_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: weakref.ReferenceType[RuntimeContextManager] | None = field(
        default=None, init=False
    )
    # snapshot 是 TypedDict（运行时即 dict），无法被弱引用包裹，因此按 task 维度强引用缓存，
    # 由 ``unload_snapshot`` / 进程内清理显式释放。
    _snapshot: ConversationStateSnapshot | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)
    _snapshot_guard: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        """初始化统一执行闸门和延迟创建的 context 槽位。"""

        self.lock = threading.Lock()
        self._context_guard = threading.Lock()
        self._snapshot_guard = threading.Lock()

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
            首次调用在 ``_snapshot_guard`` 下重建并缓存进程内 working copy；后续调用复用同一
            task 的副本，不再读取数据库或重复重建，直到 ``unload_snapshot`` 或进程内清理。
        """

        with self._snapshot_guard:
            if self._snapshot is None:
                self._snapshot = copy.deepcopy(loader())
            return copy.deepcopy(self._snapshot)

    def existing_snapshot(self) -> ConversationStateSnapshot | None:
        """返回已物化的 task snapshot，不触发数据库读取或懒加载。

        尚未物化或已被 ``unload_snapshot`` 清理时返回 None。
        """

        with self._snapshot_guard:
            return copy.deepcopy(self._snapshot) if self._snapshot is not None else None

    def replace_snapshot(self, snapshot: ConversationStateSnapshot) -> None:
        """替换 task 的进程内 snapshot working copy。

        仅供已完成 canonical 数据库提交后的显式重建或 Transport projector 使用；不会
        写数据库，也不会通知 SSE subscriber。
        """

        with self._snapshot_guard:
            self._snapshot = copy.deepcopy(snapshot)

    def unload_snapshot(self) -> None:
        """卸载 task snapshot，使下一次访问重新从 canonical records 懒加载。"""

        with self._snapshot_guard:
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
            无（构造失败会向上抛出 ``RuntimeContextManager`` 构造期的异常）。

        副作用:
            首次调用时在持有 ``_context_guard`` 的前提下惰性构造并以**弱引用**缓存 context
            manager；后续调用直接返回仍存活的缓存实例。当外部不再持有该 manager 时弱引用
            会自然失效，下次访问重新创建。
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
                from app.service.depends import get_conversation_task_context_service

                manager = (
                    RuntimeContextManager(
                        current_task_id=current_task.id,
                        agent_profile=agent_profile,
                        workspace_root=current_workspace.root_path,
                        context_service=get_conversation_task_context_service(),
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
        """

        with self._context_guard:
            return self._context_manager() if self._context_manager is not None else None

    def install_fork_context_manager(self, manager: RuntimeContextManager) -> None:
        """安装已由源 manager fork 出来的目标 context manager。"""

        with self._context_guard:
            if self._context_manager is not None and self._context_manager() is not None:
                return
            installed = (
                manager.add_change_listener(ContextUsageComputeListener(self.task_id))
                .add_change_listener(ContextCompressListener())
            )
            self._context_manager = _weak_ref(installed)
