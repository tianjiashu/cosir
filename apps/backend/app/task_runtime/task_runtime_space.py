"""一个持久化 Task 对应的运行时资源空间。

本模块只拥有统一的执行闸门（Task 操作锁）与延迟创建的运行时 context 投影；持久化的
task / run / context 记录仍由 SQLite 负责。多个 space 的进程内生命周期管理见
``app.task_runtime.task_runtime_space_registry``。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.context.context_listener.context_compress_listener import ContextCompressListener
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.service.depends import get_task_service

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord


@dataclass
class TaskRuntimeSpace:
    """一个持久化 Task 对应的运行时资源空间。"""

    task_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: RuntimeContextManager | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        """初始化统一执行闸门和延迟创建的 context 槽位。"""

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
            首次调用时在持有 ``_context_guard`` 的前提下惰性构造并缓存 context manager；
            后续调用直接返回缓存实例。
        """

        manager = self._context_manager
        if manager is not None:
            return manager
        with self._context_guard:
            manager = self._context_manager
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
            self._context_manager = manager
            return manager

    def ensure_context_usage_projection(self, run: ConversationRunRecord) -> bool:
        """在本进程首次读取 Task 时重算并投影已持久化 context 占用。"""

        with self._context_guard:
            if self._context_manager is not None:
                return False

        from app.config.configuration import get_agent_registry
        from app.service.depends import get_workspace_service

        task = get_task_service().get_task(run.task_id)
        workspace = get_workspace_service().get_workspace(task.workspace_id)
        profile = get_agent_registry().resolve(run.agent_id or "main_agent")
        if profile is None:
            log.warning(
                "context_usage_projection_skipped",
                extra={
                    "msg": "无法解析 context 重投影所需的 agent profile",
                    "data": {"task_id": run.task_id, "run_id": run.id},
                },
            )
            return False
        manager = self.get_context_manager(
            agent_profile=profile,
            current_workspace=workspace,
            current_task=task,
        )
        manager.reproject_context_usage(run)
        return True

    def _update_task_context_usage(self, task_id: int, used: int) -> None:
        """以旁路方式更新 task 上下文占用，失败只记录日志。"""

        try:
            get_task_service().update_context_usage(task_id, used)
        except Exception as exc:
            log.error(
                "context_usage_task_update_failed",
                extra={
                    "msg": "context usage write-back failed",
                    "data": {"task_id": task_id, "used": used, "error": str(exc)},
                },
                exc_info=True,
            )

    def existing_context_manager(self) -> RuntimeContextManager | None:
        """返回已物化的 context manager；不因查询而触发懒加载。"""

        with self._context_guard:
            return self._context_manager

    def install_fork_context_manager(self, manager: RuntimeContextManager) -> None:
        """安装已由源 manager fork 出来的目标 context manager。"""

        with self._context_guard:
            if self._context_manager is not None:
                return
            self._context_manager = (
                manager.add_change_listener(ContextUsageComputeListener(self.task_id))
                .add_change_listener(ContextCompressListener())
            )
