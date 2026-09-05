"""一个持久化 Task 对应的运行时资源空间。

本模块只拥有执行闸门（task 锁）与延迟创建的运行时 context 投影；持久化的
task / run / context 记录仍由 SQLite 负责。锁原语见 ``app.task_runtime.task_runtime_lock``，
多个 space 的进程内生命周期管理见 ``app.task_runtime.task_runtime_space_registry``。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.config.logging.logger import log
from app.core.context.context_listener.context_compress_listener import ContextCompressListener
from app.core.context.context_listener.context_usage_compute_listener import ContextUsageComputeListener
from app.service.depends import get_task_service

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.models import TaskRecord, WorkspaceRecord


@dataclass
class TaskRuntimeSpace:
    """一个持久化 Task 对应的运行时资源空间。"""

    task_id: int
    lock: threading.Lock = field(init=False)
    _context_manager: RuntimeContextManager | None = field(default=None, init=False)
    _context_guard: threading.Lock = field(init=False)

    def __post_init__(self) -> None:
        """初始化执行闸门和延迟创建的 context 槽位。"""

        self.lock = threading.Lock()
        self._context_guard = threading.Lock()

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

                manager = RuntimeContextManager(
                    current_task_id=current_task.id,
                    agent_profile=agent_profile,
                    workspace_root=current_workspace.root_path,
                    context_service=get_conversation_task_context_service(),
                ).add_change_listener(ContextUsageComputeListener(
                    current_task.id,
                )).add_change_listener(ContextCompressListener())
                self._context_manager = manager
            return manager

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
