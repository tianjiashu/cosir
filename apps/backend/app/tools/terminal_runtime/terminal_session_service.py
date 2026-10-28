"""Task 级终端用例与会话集合的唯一所有者。

service 负责归属、容量、创建 / 删除、外部通知与副作用线程；单会话执行由 runtime 协调，交互
通道由 ``terminal_stream`` 管理。活会话与有限终态历史只存在当前后端进程，不持久化、不跨重启恢复。

线程边界：
- worker 读线程只做内存更新；会话终态的 worker 清理在独立线程完成，不阻塞读线程；
- ``on_task_sessions_changed`` 与交接结论投递都经单线程副作用执行器在锁外串行执行，
  因此调用方（工具线程、HTTP、WS 所在事件循环）都不会被外部回调阻塞。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.observation_render import TerminalObservationResult
from app.tools.terminal_runtime.process_terminal_worker import ProcessTerminalWorker
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.terminal_runtime.terminal_input import TerminalWait
from app.tools.terminal_runtime.terminal_session_runtime import (
    RunCancellationCheck,
    SessionTermination,
    TerminalSessionRuntime,
    UserInputResult,
)
from app.tools.terminal_runtime.terminal_stream import (
    TerminalStreamAttachment,
    TerminalStreamSubscription,
)
from app.tools.tool_handler.security.path_resolver import PathResolver

_ACTIVE = frozenset({"starting", "running"})


class TerminalSessionService:
    """终端归 Task 所有，Run 结束不关闭；Task 删除及后端退出负责资源兜底。"""

    def __init__(self, on_task_sessions_changed: Callable[[int], None]) -> None:
        """``on_task_sessions_changed`` 是必需项：会话列表变化必须能投影到 Transport snapshot。

        任何影响 ``project_task_sessions`` 结果的变化（生命周期、集成就绪、租约持有方、交接）都以
        task_id 回调；回调在副作用线程、锁外执行，失败只记录日志。
        """
        self._on_task_sessions_changed = on_task_sessions_changed
        self._registry: dict[str, TerminalSessionRuntime] = {}
        self._ended: OrderedDict[str, TerminalSessionRuntime] = OrderedDict()
        self._registry_lock = threading.RLock()
        self._closing_tasks: set[int] = set()
        self._accepting = True
        self._effects = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-effects")

    # ------------------------------------------------------------------ 创建与关闭

    def start(
        self,
        *,
        task_id: int,
        workspace_root: str,
        shell: str = "auto",
        cwd: str | None = None,
        label: str | None = None,
        integration: str = "auto",
        created_by: Literal["agent", "user"],
        run_id: int | None,
    ) -> dict[str, object]:
        """创建会话并返回 Agent 侧元数据；Agent 新建另受每 Task 上限，为用户保留名额。"""
        cwd_path = self._resolve_cwd(workspace_root, cwd)
        spec = ShellResolver.resolve(shell, integration=integration)
        instance_id = uuid.uuid4().hex
        session_id = f"term_{uuid.uuid4().hex[:12]}"
        with self._registry_lock:
            self._ensure_accepting()
            if task_id in self._closing_tasks:
                raise TerminalSessionStateError("task is already closing")
            active = [runtime for runtime in self._registry.values() if runtime.status in _ACTIVE]
            if len(active) >= Constant.Terminal.MAX_ACTIVE_SESSIONS:
                raise TerminalSessionCapacityError("maximum active terminal session count reached")
            if created_by == "agent" and (
                sum(r.task_id == task_id and r.created_by == "agent" for r in active)
                >= Constant.Terminal.MAX_AGENT_SESSIONS_PER_TASK
            ):
                raise TerminalSessionCapacityError(
                    "this task already has the maximum number of agent terminals; "
                    "reuse one from terminal_list or close one first"
                )
            # worker 路径只由宿主注入的环境变量提供，由 worker 构造时自行解析。
            runtime = TerminalSessionRuntime(
                ProcessTerminalWorker(instance_id),
                session_id=session_id,
                task_id=task_id,
                created_by=created_by,
                initial_cwd=str(cwd_path),
                shell_kind=spec.kind,
                label=label,
                integration=integration,
                schedule=self._schedule,
                on_changed=lambda: self._notify_changed(task_id),
            )
            # 先登记、后握手：握手期间的会话列表刷新已带上新会话，前端按 HTTP 返回的 id 选中时不会被
            # 一帧尚不含它的列表冲掉（见前端 terminal-workbench-store 的 reconcileActive）。勿改为握手后登记。
            self._registry[session_id] = runtime
        log.info(
            "terminal_session_creating",
            extra={
                "msg": "创建终端会话",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "session_id": session_id,
                    "created_by": created_by,
                    "shell_kind": spec.kind,
                },
            },
        )

        def on_event(event: Mapping[str, object]) -> None:
            self._on_worker_event(runtime, event)

        try:
            runtime.start(spec, on_event)
            with self._registry_lock:
                stale = not self._accepting or task_id in self._closing_tasks
            if stale:
                # 批量清理已接管句柄（关闭等握手结束，见 dispose_worker）；
                # 这里不能另取句柄重复关闭。
                raise TerminalSessionStateError("task or backend is closing")
        except Exception as exc:
            self._complete_termination(
                runtime, runtime.finish("failed", f"worker_start_failed: {type(exc).__name__}")
            )
            raise
        if integration == "auto" and created_by == "agent":
            # Agent 需要立刻知道能否用 command_returned；用户新建不等，集成就绪后经快照通知。
            runtime.wait_for_integration()
        return runtime.describe()

    def close(self, session_id: str, *, task_id: int) -> dict[str, object]:
        """重复关闭只返回终态；显式关闭的资源清理失败向调用者报告。"""
        runtime = self._require_runtime(session_id, task_id)
        error = self._complete_termination(runtime, runtime.finish("closed", "closed"))
        if error is not None:
            raise error
        return runtime.describe()

    def shutdown(self) -> None:
        """后端退出：结束全部会话并排空副作用。

        结束会话会唤醒后台等待线程与交接；它们的结论经 ``deferred_results.complete`` 投递，lifespan
        已先 ``close`` 登记表，因此这里不会再写 ``task_messages``。
        """
        with self._registry_lock:
            self._accepting = False
        self._close_matching(lambda runtime: True, reason="backend_shutdown")
        with self._registry_lock:
            self._ended.clear()
        self._effects.shutdown(wait=True)

    def delete_task_sessions(self, task_ids: Iterable[int]) -> int:
        ids = set(task_ids)
        with self._registry_lock:
            self._closing_tasks.update(ids)
        count = self._close_matching(lambda runtime: runtime.task_id in ids, reason="task_deleted")
        with self._registry_lock:
            for session_id, runtime in tuple(self._ended.items()):
                if runtime.task_id in ids:
                    self._ended.pop(session_id)
        return count

    # ------------------------------------------------------------------ Agent 操作

    def write(
        self,
        session_id: str,
        *,
        task_id: int,
        operation_id: str,
        input: Mapping[str, Any],
        wait: TerminalWait | None,
        run_id: int | None,
        tool_call_id: str,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """worker 不可用（含信号回执超时）只作为本次调用的错误返回，不结束会话。

        会话终态只来自 worker 事件流（``exit`` / 致命 ``error`` / 读线程断开）与显式关闭：worker
        真的死了，读线程会补发致命错误收敛会话；在命令路径上判死会误伤正常会话（回执慢就关掉
        用户的 shell）。
        """
        runtime = self._require_runtime(session_id, task_id)
        self._ensure_accepting()
        return runtime.write(
            input,
            operation_id,
            wait,
            run_id=run_id,
            tool_call_id=tool_call_id,
            is_cancelled=is_cancelled,
        )

    def read(
        self,
        session_id: str,
        *,
        task_id: int,
        start_line: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        wait: TerminalWait | None = None,
        run_id: int | None,
        tool_call_id: str,
        is_cancelled: RunCancellationCheck,
    ) -> TerminalObservationResult:
        """长等待（超过前台上限）以 ``tool_call_id`` 登记延迟结果，见 ``deferred_results``。"""
        return self._require_runtime(session_id, task_id).read(
            start_line=start_line,
            limit=limit,
            tail=tail,
            wait=wait,
            run_id=run_id,
            tool_call_id=tool_call_id,
            is_cancelled=is_cancelled,
        )

    def list_sessions(
        self, *, task_id: int, include_ended: bool = False
    ) -> list[dict[str, object]]:
        return [runtime.describe() for runtime in self._task_runtimes(task_id, include_ended)]

    # ------------------------------------------------------------------ 用户与交互通道

    def project_task_sessions(self, task_id: int) -> list[dict[str, object]]:
        """Transport snapshot 条目：活跃会话在前、按创建时间排序，已结束会话最多 10 个。"""
        return [runtime.snapshot_entry() for runtime in self._task_runtimes(task_id, True)]

    def user_input(self, session_id: str, *, task_id: int, data: bytes) -> UserInputResult:
        """与 :meth:`write` 相同，worker 不可用只抛给调用方，不结束会话。"""
        return self._require_runtime(session_id, task_id).user_input(data)

    def resize(self, session_id: str, *, task_id: int, rows: int, cols: int) -> None:
        self._require_runtime(session_id, task_id).resize(rows, cols)

    def release_user_input(self, session_id: str, *, task_id: int) -> None:
        self._require_runtime(session_id, task_id).release_user_input()

    def complete_handoff(self, session_id: str, *, task_id: int) -> None:
        self._require_runtime(session_id, task_id).complete_handoff()

    def subscribe(
        self,
        session_id: str,
        *,
        task_id: int,
        after_seq: int | None,
        loop: asyncio.AbstractEventLoop,
    ) -> TerminalStreamAttachment:
        return self._require_runtime(session_id, task_id).subscribe(after_seq, loop)

    def resync_event(
        self, session_id: str, *, task_id: int, after_seq: int | None
    ) -> dict[str, object]:
        return self._require_runtime(session_id, task_id).resync_event(after_seq)

    def unsubscribe(self, session_id: str, subscription: TerminalStreamSubscription) -> None:
        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._ended.get(session_id)
        if runtime is not None:
            runtime.unsubscribe(subscription)
        subscription.close()

    def active_session_count(self) -> int:
        with self._registry_lock:
            return sum(runtime.status in _ACTIVE for runtime in self._registry.values())

    # ------------------------------------------------------------------ 内部

    def _task_runtimes(self, task_id: int, include_ended: bool) -> list[TerminalSessionRuntime]:
        with self._registry_lock:
            active = [r for r in self._registry.values() if r.task_id == task_id]
            ended = (
                [r for r in self._ended.values() if r.task_id == task_id] if include_ended else []
            )
        active.sort(key=lambda runtime: runtime.created_at)
        ended = ended[-Constant.Terminal.MAX_ENDED_SESSIONS_PER_TASK :]
        return [*active, *ended]

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
        self, runtime: TerminalSessionRuntime, event: Mapping[str, object]
    ) -> None:
        """worker 读线程回调：终态收尾（worker.close 会等待进程退出）移到独立线程。"""
        self._complete_termination_async(runtime, runtime.on_worker_event(event))

    def _complete_termination_async(
        self, runtime: TerminalSessionRuntime, termination: SessionTermination | None
    ) -> None:
        if termination is None:
            return
        threading.Thread(
            target=self._complete_termination,
            args=(runtime, termination),
            name=f"terminal-dispose-{runtime.session_id[-8:]}",
            daemon=True,
        ).start()

    def _complete_termination(
        self, runtime: TerminalSessionRuntime, termination: SessionTermination | None
    ) -> Exception | None:
        """统一锁外副作用；迁移赢家才拥有句柄和通知，其他路径不得重复清理。"""
        if termination is None:
            return None
        error = None
        try:
            if termination.worker is not None:
                runtime.dispose_worker(termination.worker)
        except Exception as exc:
            error = exc
            log.exception(
                "terminal_session_close_worker_failed",
                extra={
                    "msg": "Terminal Worker 清理失败",
                    "data": {"session_id": runtime.session_id, "reason": termination.reason},
                },
            )
        finally:
            # 批量清理等的是资源释放尝试完成，不等待可能阻塞或重入的观察者。
            termination.disposal_finished.set()
        self._retire_runtime(runtime)
        self._notify_changed(runtime.task_id)
        return error

    def _retire_runtime(self, runtime: TerminalSessionRuntime) -> None:
        session_id = runtime.session_id
        with self._registry_lock:
            if self._registry.get(session_id) is runtime:
                self._registry.pop(session_id)
            if not self._accepting or runtime.task_id in self._closing_tasks:
                return
            self._ended[session_id] = runtime
            self._ended.move_to_end(session_id)
            task_ended = [sid for sid, r in self._ended.items() if r.task_id == runtime.task_id]
            for stale in task_ended[: -Constant.Terminal.MAX_ENDED_SESSIONS_PER_TASK]:
                self._ended.pop(stale)
            while len(self._ended) > Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS:
                self._ended.popitem(last=False)

    def _schedule(self, effect: Callable[[], None]) -> None:
        """副作用串行执行；关闭后的提交直接丢弃（进程正在退出，通知已无接收方）。"""
        try:
            self._effects.submit(self._run_effect, effect)
        except RuntimeError:
            log.info(
                "terminal_effect_dropped_after_shutdown",
                extra={"msg": "终端服务已关闭，丢弃副作用", "data": {}},
            )

    @staticmethod
    def _run_effect(effect: Callable[[], None]) -> None:
        try:
            effect()
        except Exception:
            log.exception(
                "terminal_effect_failed",
                extra={"msg": "终端副作用执行失败（通知或结论投递）", "data": {}},
            )

    def _notify_changed(self, task_id: int) -> None:
        self._schedule(lambda: self._on_task_sessions_changed(task_id))

    def _require_runtime(self, session_id: str, task_id: int) -> TerminalSessionRuntime:
        with self._registry_lock:
            runtime = self._registry.get(session_id) or self._ended.get(session_id)
        if runtime is None:
            raise TerminalSessionNotFoundError(session_id)
        if runtime.task_id != task_id:
            raise TerminalSessionOwnershipError("terminal session does not belong to task")
        return runtime

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
