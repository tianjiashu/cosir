"""终端会话集合的唯一所有者：归属、容量、创建 / 关闭、终态历史与副作用线程。

单会话操作（读取、填入、订阅、用户输入）由调用方经 ``require`` 取到 runtime 后直接调用，本服务
不做转发。会话只由用户新建（工作台 HTTP），Agent 工具只列出、读取与填入；两侧共用这里的归属与
生命周期规则，所以集合管理不能放进任何一侧。活会话与有限终态历史只存在当前后端进程，不持久化、
不跨重启恢复。

线程边界：
- worker 读线程只做内存更新；会话终态的 worker 清理在独立线程完成，不阻塞读线程；
- 会话变化经装配时注入的 ``on_sessions_changed`` 交给应用层重投影 snapshot，由单线程副作用执行器
  在锁外串行执行，因此调用方（工具线程、HTTP、WS）都不会被外部副作用阻塞，运行时也不认识应用层。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.terminal_runtime.errors import (
    TerminalSessionCapacityError,
    TerminalSessionNotFoundError,
    TerminalSessionOwnershipError,
    TerminalSessionStateError,
)
from app.tools.terminal_runtime.session.session_runtime import (
    TerminalSessionRuntime,
    TerminalSessionSnapshot,
)
from app.tools.terminal_runtime.session.shell_resolver import resolve_session_shell


class TerminalSessionService:
    """终端归 Session 所有，Run 结束不关闭；Session 删除及后端退出负责资源兜底。"""

    def __init__(self, *, on_sessions_changed: Callable[[int], None]) -> None:
        """``on_sessions_changed(session_id)`` 在副作用线程、锁外调用：该 Session 的会话条目变了。"""
        self._on_sessions_changed = on_sessions_changed
        self._registry: dict[str, TerminalSessionRuntime] = {}
        self._ended: OrderedDict[str, TerminalSessionRuntime] = OrderedDict()
        self._registry_lock = threading.RLock()
        self._closing_sessions: set[int] = set()
        self._accepting = True
        self._effects = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-effects")

    # ------------------------------------------------------------------ 创建与关闭

    def start(
        self,
        *,
        session_id: int,
        cwd: str,
        shell: str = "auto",
        integration: str = "auto",
    ) -> TerminalSessionRuntime:
        """在 ``cwd``（调用方已确认存在的目录，用户会话是工作区根）创建会话并完成 worker 握手；
        容量受全进程活跃会话上限约束。"""
        spec = resolve_session_shell(shell, integration=integration)
        with self._registry_lock:
            if not self._accepting:
                raise TerminalSessionStateError("terminal session service is shutting down")
            if session_id in self._closing_sessions:
                raise TerminalSessionStateError("session is already closing")
            active = [runtime for runtime in self._registry.values() if runtime.active]
            if len(active) >= Constant.Terminal.MAX_ACTIVE_SESSIONS:
                raise TerminalSessionCapacityError("maximum active terminal session count reached")
            runtime = TerminalSessionRuntime(
                session_id=session_id,
                initial_cwd=cwd,
                spec=spec,
                notify_sessions_changed=self._notify_sessions_changed,
            )
            terminal_session_id = runtime.terminal_session_id
            # 先登记、后握手：握手期间的会话列表刷新已带上新会话，前端按 HTTP 返回的 id 选中时
            # 不会被一帧尚不含它的列表冲掉（见前端 terminal-workbench-store 的 reconcileActive）。
            # 勿改为握手后登记。
            self._registry[terminal_session_id] = runtime
        log.info(
            "terminal_session_creating",
            extra={
                "msg": "创建终端会话",
                "data": {
                    "session_id": session_id,
                    "terminal_session_id": terminal_session_id,
                    "shell_kind": spec.kind,
                },
            },
        )

        def on_event(event: Mapping[str, object]) -> None:
            self._on_worker_event(runtime, event)

        try:
            runtime.start(on_event)
            with self._registry_lock:
                stale = not self._accepting or session_id in self._closing_sessions
            if stale:
                # 批量清理已赢得终态迁移并负责关闭（等握手结束，见 dispose_worker）；
                # 这里的 finish 不会再赢，不会重复关闭。
                raise TerminalSessionStateError("session or backend is closing")
        except Exception as exc:
            reason = f"worker_start_failed: {type(exc).__name__}"
            if runtime.finish("failed", reason):
                self._complete_termination(runtime)
            raise
        return runtime

    def close(self, terminal_session_id: str, *, session_id: int) -> TerminalSessionSnapshot:
        """重复关闭只返回终态；显式关闭的资源清理失败向调用者报告。

        会话只由用户在终端面板关闭，原因恒为 ``closed_by_user``；Session 删除与后端退出走批量清理。
        """
        runtime = self.require(terminal_session_id, session_id)
        if runtime.finish("closed", "closed_by_user"):
            error = self._complete_termination(runtime)
            if error is not None:
                raise error
        return runtime.snapshot()

    def shutdown(self) -> None:
        """后端退出：结束全部会话（唤醒进行中的读取等待）并排空副作用。"""
        with self._registry_lock:
            self._accepting = False
        self._close_matching(lambda runtime: True, reason="backend_shutdown")
        with self._registry_lock:
            self._ended.clear()
        self._effects.shutdown(wait=True)

    def delete_session_terminals(self, session_ids: Iterable[int]) -> int:
        ids = set(session_ids)
        with self._registry_lock:
            self._closing_sessions.update(ids)
        count = self._close_matching(lambda runtime: runtime.session_id in ids, reason="session_deleted")
        with self._registry_lock:
            for terminal_session_id, runtime in tuple(self._ended.items()):
                if runtime.session_id in ids:
                    self._ended.pop(terminal_session_id)
        return count

    # ------------------------------------------------------------------ 查询

    def require(self, terminal_session_id: str, session_id: int) -> TerminalSessionRuntime:
        """按 Session 归属取会话（活会话或保留的终态会话），单会话操作由调用方直接调 runtime。

        异常:
            TerminalSessionNotFoundError: 会话不存在（含后端关闭后，集合已清空）。
            TerminalSessionOwnershipError: 会话不属于该 Session。
        """
        runtime = self._lookup(terminal_session_id)
        if runtime is None:
            raise TerminalSessionNotFoundError(f"session {terminal_session_id} no longer exists")
        if runtime.session_id != session_id:
            raise TerminalSessionOwnershipError("terminal session does not belong to session")
        return runtime

    def session_runtimes(self, session_id: int) -> list[TerminalSessionRuntime]:
        """活跃会话按创建时间在前，随后是最近结束的会话（入历史时已按 Session 裁到
        ``MAX_ENDED_SESSIONS_PER_SESSION`` 个，见 :meth:`_retire_runtime`）。

        结束的会话也列出，让 Agent 与前端知道它已退出。
        """
        with self._registry_lock:
            active = [r for r in self._registry.values() if r.session_id == session_id]
            ended = [r for r in self._ended.values() if r.session_id == session_id]
        active.sort(key=lambda runtime: runtime.created_at)
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
                self._registry.pop(runtime.terminal_session_id, None)
        for runtime in runtimes:
            if runtime.finish("closed", reason):
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
            name=f"terminal-dispose-{runtime.terminal_session_id[-8:]}",
            daemon=True,
        ).start()

    def _complete_termination(self, runtime: TerminalSessionRuntime) -> Exception | None:
        """终态迁移赢家的锁外收尾（只调用一次）：释放 worker、归档、通知。

        返回清理异常供显式关闭上报；终态原因已由 runtime 的 ``terminal_session_ended`` 日志按
        terminal_session_id 记录，这里不重复。
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
                    "data": {"terminal_session_id": runtime.terminal_session_id},
                },
            )
        self._retire_runtime(runtime)
        runtime.notify_changed()
        return error

    def _retire_runtime(self, runtime: TerminalSessionRuntime) -> None:
        terminal_session_id = runtime.terminal_session_id
        with self._registry_lock:
            if self._registry.get(terminal_session_id) is runtime:
                self._registry.pop(terminal_session_id)
            if not self._accepting or runtime.session_id in self._closing_sessions:
                return
            self._ended[terminal_session_id] = runtime
            self._ended.move_to_end(terminal_session_id)
            session_ended = [sid for sid, r in self._ended.items() if r.session_id == runtime.session_id]
            for stale in session_ended[: -Constant.Terminal.MAX_ENDED_SESSIONS_PER_SESSION]:
                self._ended.pop(stale)
            while len(self._ended) > Constant.Terminal.MAX_TERMINAL_HISTORY_SESSIONS:
                self._ended.popitem(last=False)

    def _notify_sessions_changed(self, session_id: int) -> None:
        """会话条目变化的通知串行交给副作用线程；关闭后的提交直接丢弃（进程正在退出，已无接收方）。"""
        try:
            self._effects.submit(self._run_effect, lambda: self._on_sessions_changed(session_id))
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
                extra={"msg": "终端副作用执行失败（会话列表重投影）", "data": {}},
            )

    def _lookup(self, terminal_session_id: str) -> TerminalSessionRuntime | None:
        """活会话或保留的终态会话。"""
        with self._registry_lock:
            return self._registry.get(terminal_session_id) or self._ended.get(terminal_session_id)
