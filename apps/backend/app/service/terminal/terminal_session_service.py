"""Task 级终端用例与会话集合的唯一所有者。

service 负责归属、容量、创建/删除及外部状态通知；单会话执行由 runtime 协调，
预览流由 preview 管理。活会话和有限终态历史只存在当前后端进程，不进入 checkpoint。
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tools.tool_handler.security.path_resolver import PathResolver
from app.service.terminal.errors import (
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
    TerminalWorkerUnavailableError,
)
from app.service.terminal.process_terminal_worker import ProcessTerminalWorker
from app.service.terminal.shell_resolver import ShellResolver
from app.service.terminal.terminal_preview import (
    TerminalPreviewAttachment,
    TerminalPreviewSubscription,
)
from app.service.terminal.terminal_session_runtime import (
    RunCancellationCheck,
    SessionTermination,
    TerminalSessionRuntime,
)
from app.service.terminal.terminal_session_status import (
    TerminalSessionStatusChange,
    TerminalSessionStatusObserverLike,
)


class TerminalSessionService:
    """终端归 Task 所有，Run 结束不关闭；Task 删除及后端退出负责资源兜底。"""

    def __init__(self, status_observer: TerminalSessionStatusObserverLike) -> None:
        """绑定状态回写观察者；装配层必须显式传入，不做默认降级。

        观察者是显式必需项而非可选钩子：终端生命周期变化必须能投影到 Transport，
        缺失就让 TypeError 在装配期暴露，避免出现「会话状态没人回写」的静默失效。
        """

        self._max_active_sessions = Constant.Terminal.MAX_ACTIVE_SESSIONS
        self._ring_buffer_bytes = Constant.Terminal.MAX_RING_BUFFER_BYTES
        self._status_observer: TerminalSessionStatusObserverLike = status_observer
        self._registry: dict[str, TerminalSessionRuntime] = {}
        self._terminal_history: OrderedDict[str, TerminalSessionRuntime] = OrderedDict()
        self._registry_lock = threading.RLock()
        self._closing_tasks: set[int] = set()
        self._accepting = True

    def start(
        self,
        *,
        task_id: int,
        workspace_id: int,
        workspace_root: str,
        shell: str = "auto",
        cwd: str | None = None,
        label: str | None = None,
        run_id: int,
        integration: str = "auto",
    ) -> dict[str, object]:
        cwd_path = self._resolve_cwd(workspace_root, cwd)
        spec = ShellResolver.resolve(shell, integration=integration)
        instance_id = uuid.uuid4().hex
        session_id = f"term_{uuid.uuid4().hex}"
        with self._registry_lock:
            self._ensure_accepting()
            if task_id in self._closing_tasks:
                raise TerminalSessionStateError("task is already closing")
            active = sum(
                runtime.status in {"starting", "running"} for runtime in self._registry.values()
            )
            if active >= self._max_active_sessions:
                raise TerminalSessionCapacityError("maximum active terminal session count reached")
            # worker 路径不在 service 层配置：只由宿主启动后端时注入的环境变量
            # ``Constant.Terminal.WORKER_ENV`` 提供，由 worker 构造时自行解析；
            # 这里显式传 None 表示不覆盖宿主配置。
            runtime = TerminalSessionRuntime(
                ProcessTerminalWorker(None, instance_id),
                session_id=session_id,
                task_id=task_id,
                workspace_id=workspace_id,
                created_by_run_id=run_id,
                initial_cwd=str(cwd_path),
                shell_kind=spec.kind,
                label=label,
                integration=integration,
                buffer_limit=self._ring_buffer_bytes,
            )
            self._registry[session_id] = runtime

        def on_event(event: Mapping[str, object]) -> None:
            self._on_worker_event(session_id, runtime.generation, event)

        try:
            change = runtime.start(spec, on_event)
            with self._registry_lock:
                stale = not self._accepting or task_id in self._closing_tasks
            if stale:
                # 批量清理会等慢启动退出会话锁；这里不能另取句柄重复关闭。
                raise TerminalSessionStateError("task or backend is closing")
            if change is not None:
                self._notify_status_changed(runtime, change)
        except Exception as exc:
            self._complete_termination(
                runtime, runtime.finish("failed", f"worker_start_failed: {type(exc).__name__}")
            )
            raise
        if integration == "auto":
            runtime.wait_for_integration()
        return runtime.snapshot()

    def write(
        self,
        session_id: str,
        *,
        task_id: int,
        input: dict[str, str],
        operation_id: str,
        workspace_id: int | None = None,
        wait_ms: int = 500,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> dict[str, Any]:
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        self._ensure_accepting()
        try:
            return runtime.write(input, operation_id, wait_ms, is_cancelled)
        except TerminalWorkerUnavailableError:
            self._complete_termination(runtime, runtime.finish("failed", "worker_unavailable"))
            raise

    def read(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        cursor: str | None = None,
        wait: dict[str, Any] | None = None,
        is_cancelled: RunCancellationCheck | None = None,
    ) -> dict[str, Any]:
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        return runtime.read(cursor, wait, is_cancelled)

    def signal(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        signal_name: str,
    ) -> dict[str, object]:
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        self._ensure_accepting()
        try:
            return runtime.signal(signal_name)
        except TerminalWorkerUnavailableError:
            self._complete_termination(runtime, runtime.finish("failed", "worker_unavailable"))
            raise

    def close(
        self,
        session_id: str,
        *,
        task_id: int,
        workspace_id: int | None = None,
        reason: str = "agent_closed",
    ) -> dict[str, object]:
        """重复关闭只返回终态；显式关闭的资源清理失败向调用者报告。"""
        runtime = self._require_runtime(session_id, task_id, workspace_id=workspace_id)
        error = self._complete_termination(runtime, runtime.finish("closed", reason))
        if error is not None:
            raise error
        return runtime.snapshot()

    def snapshot(
        self, session_id: str, *, task_id: int, workspace_id: int | None = None
    ) -> dict[str, object]:
        return self._require_runtime(session_id, task_id, workspace_id=workspace_id).snapshot()

    def list_sessions(self, *, task_id: int, workspace_id: int) -> list[dict[str, object]]:
        with self._registry_lock:
            runtimes = [*self._registry.values(), *self._terminal_history.values()]
        return [
            runtime.snapshot()
            for runtime in runtimes
            if runtime.task_id == task_id and runtime.workspace_id == workspace_id
        ]

    def get_status_change(
        self, session_id: str, *, task_id: int
    ) -> TerminalSessionStatusChange | None:
        """Transport 冷重建使用；未知或不属于 Task 的会话表示当前进程无此状态。"""
        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._terminal_history.get(session_id)
        if runtime is None or runtime.task_id != task_id:
            return None
        return runtime.status_change()

    def subscribe(
        self, session_id: str, *, task_id: int, after_seq: int | None
    ) -> TerminalPreviewAttachment:
        return self._require_runtime(session_id, task_id).subscribe(after_seq)

    def unsubscribe(self, session_id: str, subscription: TerminalPreviewSubscription) -> None:
        with self._registry_lock:
            runtime = self._registry.get(session_id)
        if runtime is not None:
            runtime.unsubscribe(subscription)
        subscription.close()

    def shutdown(self) -> None:
        with self._registry_lock:
            self._accepting = False
        self._close_matching(lambda runtime: True, reason="backend_shutdown")
        with self._registry_lock:
            self._terminal_history.clear()

    def delete_task_sessions(self, task_ids: Iterable[int]) -> int:
        ids = set(task_ids)
        with self._registry_lock:
            self._closing_tasks.update(ids)
        count = self._close_matching(lambda runtime: runtime.task_id in ids, reason="task_deleted")
        with self._registry_lock:
            for session_id, runtime in tuple(self._terminal_history.items()):
                if runtime.task_id in ids:
                    self._terminal_history.pop(session_id)
        return count

    def _close_matching(
        self, predicate: Callable[[TerminalSessionRuntime], bool], *, reason: str
    ) -> int:
        """先阻断旧回调查找，再逐个收尾；单个清理失败不妨碍其余会话。"""
        with self._registry_lock:
            runtimes = [runtime for runtime in self._registry.values() if predicate(runtime)]
            for runtime in runtimes:
                self._registry.pop(runtime.session_id, None)
        for runtime in runtimes:
            self._complete_termination(runtime, runtime.finish("closed", reason))
            runtime.wait_for_disposal()
        return len(runtimes)

    def _on_worker_event(
        self, session_id: str, generation: str, event: Mapping[str, object]
    ) -> None:
        with self._registry_lock:
            runtime = self._registry.get(session_id)
        if runtime is None:
            return
        if runtime.generation != generation:
            log.warning(
                "terminal_session_stale_worker_event",
                extra={
                    "msg": "丢弃旧 Terminal Worker callback",
                    "data": {"session_id": session_id},
                },
            )
            return
        self._complete_termination(runtime, runtime.on_worker_event(event))

    def _complete_termination(
        self, runtime: TerminalSessionRuntime, termination: SessionTermination | None
    ) -> Exception | None:
        """统一锁外副作用；迁移赢家才拥有句柄和通知，其他路径不得重复清理。"""
        if termination is None:
            return None
        error = None
        try:
            if termination.worker is not None:
                termination.worker.close()
        except Exception as exc:
            error = exc
            log.exception(
                "terminal_session_close_worker_failed",
                extra={
                    "msg": "Terminal Worker 清理失败",
                    "data": {
                        "session_id": termination.change.session_id,
                        "reason": termination.change.end_reason,
                    },
                },
            )
        finally:
            # 批量清理等的是资源释放尝试完成，不等待可能阻塞或重入的观察者。
            termination.disposal_finished.set()
        self._notify_status_changed(runtime, termination.change)
        self._retire_runtime(runtime)
        return error

    def _notify_status_changed(
        self, runtime: TerminalSessionRuntime, change: TerminalSessionStatusChange
    ) -> None:
        """按会话串行通知，丢弃被终态超越的 running；不持有会话状态锁。"""
        with runtime.status_delivery_lock:
            if runtime.status != change.status:
                return
            self._deliver_status_change(runtime.created_by_run_id, change)

    def _deliver_status_change(
        self, created_by_run_id: int, change: TerminalSessionStatusChange
    ) -> None:
        """通知使用迁移时冻结的事实；外部观察者失败不阻断资源收尾。"""
        log.info(
            "terminal_session_status_changed",
            extra={
                "msg": "Task 终端生命周期变化",
                "data": {
                    "task_id": change.task_id,
                    "created_by_run_id": created_by_run_id,
                    "session_id": change.session_id,
                    "status": change.status,
                    "reason": change.end_reason,
                },
            },
        )
        try:
            self._status_observer(change)
        except Exception:
            log.exception(
                "terminal_session_status_observer_failed",
                extra={
                    "msg": "Terminal session 状态回写 Transport 失败",
                    "data": {
                        "task_id": change.task_id,
                        "session_id": change.session_id,
                        "status": change.status,
                        "reason": change.end_reason,
                    },
                },
            )

    def _retire_runtime(self, runtime: TerminalSessionRuntime) -> None:
        session_id = runtime.session_id
        with self._registry_lock:
            if self._registry.get(session_id) is runtime:
                self._registry.pop(session_id)
            if not self._accepting or runtime.task_id in self._closing_tasks:
                return
            self._terminal_history[session_id] = runtime
            self._terminal_history.move_to_end(session_id)
            while len(self._terminal_history) > Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS:
                self._terminal_history.popitem(last=False)

    def _require_runtime(
        self, session_id: str, task_id: int, *, workspace_id: int | None = None
    ) -> TerminalSessionRuntime:
        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._terminal_history.get(session_id)
            if runtime is not None:
                if runtime.task_id != task_id or (
                    workspace_id is not None and runtime.workspace_id != workspace_id
                ):
                    raise TerminalSessionOwnershipError("terminal session does not belong to task")
                return runtime
        raise TerminalSessionNotFoundError(session_id)

    @staticmethod
    def _resolve_cwd(workspace_root: str, cwd: str | None) -> Path:
        """初始 cwd 限于工作区；终端允许访问 .cosir，不叠加文件工具保留区限制。"""
        resolved, error = PathResolver(workspace_root).resolve_within_workspace(
            cwd or ".", allow_reserved=True
        )
        if resolved is None or not resolved.is_dir():
            raise TerminalSessionError(f"invalid terminal cwd: {error or cwd}")
        return resolved

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise TerminalSessionStateError("terminal session service is shutting down")
