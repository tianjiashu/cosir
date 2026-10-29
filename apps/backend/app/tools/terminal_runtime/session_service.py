"""终端会话集合的唯一所有者：归属、容量、创建 / 关闭、终态历史、外部通知与副作用线程。

单会话操作（输入、读取、交接、订阅、用户输入）由调用方经 ``require`` 取到 runtime 后直接调用，
本服务不做转发；Agent 专属策略（新建后等集成就绪、列表摘要）在工具里。Agent 工具与用户路径
（工作台 HTTP、终端 WS）共用这里的容量与生命周期规则，所以集合管理不能放进任何一侧。活会话与
有限终态历史只存在当前后端进程，不持久化、不跨重启恢复。

线程边界：
- worker 读线程只做内存更新；会话终态的 worker 清理在独立线程完成，不阻塞读线程；
- 会话变化的 snapshot 重投影、会话结束通知与交接代为落定都经单线程副作用执行器在锁外串行执行
  （副作用本身集中在 ``session_effects``），因此调用方（工具线程、HTTP、
  WS 所在事件循环）都不会被外部副作用阻塞。
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalSessionCapacityError,
    TerminalSessionError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.terminal_runtime.session_runtime import (
    SessionSnapshot,
    TerminalSessionRuntime,
)
from app.tools.tool_handler.security.path_resolver import PathResolver


class TerminalSessionService:
    """终端归 Task 所有，Run 结束不关闭；Task 删除及后端退出负责资源兜底。"""

    def __init__(self) -> None:
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
    ) -> TerminalSessionRuntime:
        """创建会话并完成 worker 握手；Agent 新建另受每 Task 上限，为用户保留名额。"""
        cwd_path = self._resolve_cwd(workspace_root, cwd)
        spec = ShellResolver.resolve(shell, integration=integration)
        with self._registry_lock:
            if not self._accepting:
                raise TerminalSessionStateError("terminal session service is shutting down")
            if task_id in self._closing_tasks:
                raise TerminalSessionStateError("task is already closing")
            active = [runtime for runtime in self._registry.values() if runtime.active]
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
            runtime = TerminalSessionRuntime(
                task_id=task_id,
                created_by=created_by,
                initial_cwd=str(cwd_path),
                spec=spec,
                label=label,
                schedule=self._schedule,
            )
            session_id = runtime.session_id
            # 先登记、后握手：握手期间的会话列表刷新已带上新会话，前端按 HTTP 返回的 id 选中时
            # 不会被一帧尚不含它的列表冲掉（见前端 terminal-workbench-store 的 reconcileActive）。
            # 勿改为握手后登记。
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
            runtime.start(on_event)
            with self._registry_lock:
                stale = not self._accepting or task_id in self._closing_tasks
            if stale:
                # 批量清理已赢得终态迁移并负责关闭（等握手结束，见 dispose_worker）；
                # 这里的 finish 不会再赢，不会重复关闭。
                raise TerminalSessionStateError("task or backend is closing")
        except Exception as exc:
            reason = f"worker_start_failed: {type(exc).__name__}"
            if runtime.finish("failed", reason, announce=False):
                self._complete_termination(runtime)
            raise
        return runtime

    def close(
        self, session_id: str, *, task_id: int, by: Literal["agent", "user"]
    ) -> SessionSnapshot:
        """重复关闭只返回终态；显式关闭的资源清理失败向调用者报告。

        用户关闭记为 ``closed_by_user`` 并告知创建它的 Agent；Agent 自己关闭不另行通知。
        """
        runtime = self.require(session_id, task_id)
        reason = "closed" if by == "agent" else "closed_by_user"
        if runtime.finish("closed", reason, announce=by == "user"):
            error = self._complete_termination(runtime)
            if error is not None:
                raise error
        return runtime.snapshot()

    def shutdown(self) -> None:
        """后端退出：结束全部会话并排空副作用。

        结束会话会唤醒前台等待；交接不在关闭时代为落定（等待中的决定按 HITL 契约保留，见
        ``TerminalSessionRuntime.finish``），因此这里不会写 ``task_messages``。
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

    # ------------------------------------------------------------------ 查询

    def require(self, session_id: str, task_id: int) -> TerminalSessionRuntime:
        """按 Task 归属取会话（活会话或保留的终态会话），单会话操作由调用方直接调 runtime。

        异常:
            TerminalSessionNotFoundError: 会话不存在（含后端关闭后，集合已清空）。
            TerminalSessionOwnershipError: 会话不属于该 Task。
        """
        runtime = self._lookup(session_id)
        if runtime is None:
            raise TerminalSessionNotFoundError(session_id)
        if runtime.task_id != task_id:
            raise TerminalSessionOwnershipError("terminal session does not belong to task")
        return runtime

    def task_runtimes(self, task_id: int) -> list[TerminalSessionRuntime]:
        """活跃会话按创建时间在前，随后是最多 ``MAX_ENDED_SESSIONS_PER_TASK`` 个最近结束的会话。

        结束的会话也列出，让 Agent 与前端知道它已退出。
        """
        with self._registry_lock:
            active = [r for r in self._registry.values() if r.task_id == task_id]
            ended = [r for r in self._ended.values() if r.task_id == task_id]
        active.sort(key=lambda runtime: runtime.created_at)
        ended = ended[-Constant.Terminal.MAX_ENDED_SESSIONS_PER_TASK :]
        return [*active, *ended]

    def active_session_count(self) -> int:
        with self._registry_lock:
            return sum(runtime.active for runtime in self._registry.values())

    # ------------------------------------------------------------------ 内部

    def _close_matching(
        self, predicate: Callable[[TerminalSessionRuntime], bool], *, reason: str
    ) -> int:
        """先阻断旧回调查找，再逐个收尾；单个清理失败不妨碍其余会话。"""
        with self._registry_lock:
            runtimes = [runtime for runtime in self._registry.values() if predicate(runtime)]
            for runtime in runtimes:
                self._registry.pop(runtime.session_id, None)
        for runtime in runtimes:
            if runtime.finish("closed", reason, announce=False):
                self._complete_termination(runtime)
            # 迁移若被其他路径（worker 事件的收尾线程）赢得，这里等它的释放尝试完成。
            runtime.wait_for_disposal()
        return len(runtimes)

    def _on_worker_event(
        self, runtime: TerminalSessionRuntime, event: Mapping[str, object]
    ) -> None:
        """worker 读线程回调：终态收尾（worker.close 会等待进程退出）移到独立线程。"""
        if not runtime.on_worker_event(event):
            return
        threading.Thread(
            target=self._complete_termination,
            args=(runtime,),
            name=f"terminal-dispose-{runtime.session_id[-8:]}",
            daemon=True,
        ).start()

    def _complete_termination(self, runtime: TerminalSessionRuntime) -> Exception | None:
        """终态迁移赢家的锁外收尾（只调用一次）：释放 worker、归档、通知。

        返回清理异常供显式关闭上报；终态原因已由 runtime 的 ``terminal_session_ended`` 日志按
        session_id 记录，这里不重复。
        """
        error = None
        try:
            runtime.dispose_worker()
        except Exception as exc:
            error = exc
            log.exception(
                "terminal_session_close_worker_failed",
                extra={
                    "msg": "Terminal Worker 清理失败",
                    "data": {"session_id": runtime.session_id},
                },
            )
        self._retire_runtime(runtime)
        runtime.notify_changed()
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

    def _lookup(self, session_id: str) -> TerminalSessionRuntime | None:
        """活会话或保留的终态会话。"""
        with self._registry_lock:
            return self._registry.get(session_id) or self._ended.get(session_id)

    @staticmethod
    def _resolve_cwd(workspace_root: str, cwd: str | None) -> Path:
        """初始 cwd 限于工作区；终端允许访问 .cosir，不叠加文件工具保留区限制。"""
        resolved, error = PathResolver(workspace_root).resolve_within_workspace(
            cwd or ".", allow_reserved=True
        )
        if resolved is None or not resolved.is_dir():
            raise TerminalSessionError(f"invalid terminal cwd: {error or cwd}")
        return resolved
