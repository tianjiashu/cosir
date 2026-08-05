"""CodeGraph Kernel 常驻子进程的生命周期管理。

单一职责：管理常驻 Kernel 子进程的生命周期——启动、握手、健康检查、崩溃重启、
关闭、stderr 日志桥接。**不理解 workspace 索引业务，不解析查询结果。**

对齐项目现有范式：
- 进程启动：``subprocess.Popen`` 启动锁定的 node ``dist/agent-kernel/server.js``
  （参照 tool_executor 隔离思路，但 Kernel 是长驻单进程，非 per-tool 子进程）；
- node 解析：``node_resolver.resolve_node_binary()``（从固定目录、缺失即报错，
  绝不读 PATH，对齐 runtime_locator 范式）；
- 关闭：发 kernel.shutdown，有限等待，超时强杀；
- stderr 桥接：经 ``config.logging`` 的 ``log`` 单例汇入后端统一日志。

Kernel 进程状态：stopped / starting / ready / degraded / restarting / failed / stopping。
"""

import subprocess
import threading
import time
from enum import Enum
from pathlib import Path

from app.codegraph.exceptions import (
    CodeGraphKernelError,
    CodeGraphKernelUnavailableError,
    CodeGraphNodeMissingError,
)
from app.codegraph.kernel_client import CodeGraphKernelClient
from app.codegraph.node_resolver import resolve_node_binary
from app.config.logging.logger import log
from app.config.settings import Settings

#: 退避重启间隔（秒）；用尽后状态置 failed。
_BACKOFF_SECONDS = (0.5, 1.0, 2.0, 5.0)
#: 健康检查周期（秒）。
_HEALTH_CHECK_INTERVAL = 10.0
#: 关闭时等待 Kernel 自行退出的上限（秒）。
_SHUTDOWN_GRACE = 5.0


class KernelState(str, Enum):
    """Kernel 进程生命周期状态（与讨论文档一致）。"""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPING = "stopping"


def _server_script_path() -> Path:
    """推导 agent-kernel 编译产出路径 ``dist/agent-kernel/server.js``。

    参数:
        无。

    返回:
        绝对路径。

    异常:
        无。

    副作用:
        无。

    路径约定:
        agent-kernel 是 ``third_party/codegraph`` vendor 内的窄适配层（源码位于
        ``third_party/codegraph/src/agent-kernel/``），随 codegraph 主工程 ``tsc``
        构建一并产出到 ``third_party/codegraph/dist/agent-kernel/server.js``
        （见 docs/codegraph-docs/codegraph-agent-kernel-design.md）。早期实现误指到
        不存在的 ``third_party/workspace_event`` 目录，导致 Kernel 启动报
        ``agent-kernel server not built``；此处以真实 vendor 路径为准。
    """
    root = Settings.repository_root()
    return root / "third_party" / "codegraph" / "dist" / "agent-kernel" / "server.js"


class CodeGraphKernelSupervisor:
    """常驻 Kernel 子进程的管理器（不持有业务语义）。"""

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        """构造管理器（不立即启动进程）。

        参数:
            timeout_seconds: 单次 Kernel RPC 默认超时（秒），透传给 Client。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self._timeout_seconds = timeout_seconds
        self._state = KernelState.STOPPED
        self._state_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._client: CodeGraphKernelClient | None = None
        self._stderr_thread: threading.Thread | None = None
        self._health_thread: threading.Thread | None = None
        self._stop_health = threading.Event()
        self._restart_attempts = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> KernelState:
        """当前进程状态（线程安全读）。"""
        with self._state_lock:
            return self._state

    def start(self) -> None:
        """启动 Kernel：解析 node → 拉起进程 → 握手 → 进入 ready。

        参数:
            无。

        返回:
            无。

        异常:
            CodeGraphNodeMissingError: 锁定的 node 缺失（前置错误，不重试）。
            CodeGraphKernelError（子类）: 握手失败等其他启动错误。

        副作用:
            创建子进程、Client、stderr/health 线程；进程状态流转
            stopped → starting → ready（或 degraded → 重试）。
        """
        with self._state_lock:
            if self._state not in (
                KernelState.STOPPED,
                KernelState.FAILED,
                KernelState.RESTARTING,
            ):
                return
            self._state = KernelState.STARTING

        try:
            self._spawn()
        except CodeGraphNodeMissingError:
            self._set_state(KernelState.FAILED)
            raise
        except OSError as exc:
            log.error(
                "codegraph_kernel_spawn_failed",
                extra={"msg": "Kernel 进程启动失败", "data": {"error": str(exc)}},
            )
            self._set_state(KernelState.FAILED)
            raise CodeGraphKernelUnavailableError(f"failed to spawn kernel: {exc}") from exc

        try:
            self._handshake()
        except CodeGraphKernelError as exc:
            log.error(
                "codegraph_kernel_handshake_failed",
                extra={"msg": "Kernel 握手失败", "data": {"error": str(exc)}},
            )
            self._handle_crash()
            raise

        with self._state_lock:
            self._restart_attempts = 0
        self._set_state(KernelState.READY)
        self._start_health_check()
        log.info("codegraph_kernel_ready", extra={"msg": "Kernel 已就绪"})

    def shutdown(self) -> None:
        """优雅关闭 Kernel：发 kernel.shutdown → 等待 → 超时强杀。

        参数:
            无。

        返回:
            无。

        异常:
            无（关闭失败仅记录日志，最终仍清理资源）。

        副作用:
            置 stopping；停止 health/stderr 线程；终止子进程。
        """
        with self._state_lock:
            if self._state in (KernelState.STOPPED, KernelState.STOPPING):
                return
            self._state = KernelState.STOPPING

        self._stop_health.set()
        if self._client is not None:
            self._client.shutdown()
            self._client.stop()

        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=_SHUTDOWN_GRACE)
            except subprocess.TimeoutExpired:
                log.warning(
                    "codegraph_kernel_force_kill",
                    extra={"msg": "Kernel 未在宽限期内退出，强制强杀"},
                )
                self._force_kill(proc)
        # 无论 proc 是否为 None，统一收尾线程与句柄，避免残留已停止的 client 引用。
        self._join_threads()
        self._proc = None
        self._client = None
        self._set_state(KernelState.STOPPED)

    def get_client(self) -> CodeGraphKernelClient:
        """返回已就绪的 Client；未就绪抛错。

        参数:
            无。

        返回:
            已握手的 ``CodeGraphKernelClient``。

        异常:
            CodeGraphKernelUnavailableError: 状态非 ready 或 Client 为空。

        副作用:
            无。
        """
        state = self.state
        if state != KernelState.READY or self._client is None:
            raise CodeGraphKernelUnavailableError(f"kernel not ready (state={state.value})")
        return self._client

    # ------------------------------------------------------------------
    # Internal: spawn / handshake / health
    # ------------------------------------------------------------------

    def _spawn(self) -> None:
        """解析 node 并拉起 Kernel 子进程，启动 stderr 桥接线程。"""
        node = resolve_node_binary()
        server_js = _server_script_path()
        if not server_js.is_file():
            raise CodeGraphNodeMissingError(
                f"agent-kernel server not built at {server_js}; run the vendor build first."
            )
        # node 与 server_js 均来自固定目录解析（node_resolver），非用户输入，可信。
        self._proc = subprocess.Popen(  # noqa: S603
            [str(node), str(server_js)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._client = CodeGraphKernelClient(self._proc, timeout_seconds=self._timeout_seconds)
        self._start_stderr_bridge()

    def _handshake(self) -> None:
        """与 Kernel 完成 kernel.hello 握手（含协议版本校验）。"""
        assert self._client is not None
        self._client.hello()

    def _start_stderr_bridge(self) -> None:
        """启动 stderr 读取线程，把 Kernel 内部日志汇入后端统一日志。"""
        assert self._proc is not None
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="workspace_event-kernel-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _stderr_loop(self) -> None:
        """消费 Kernel stderr 行，逐行写入后端日志。"""
        assert self._proc is not None
        stderr = self._proc.stderr
        if stderr is None:
            return
        for raw in stderr:
            line = raw.rstrip("\n")
            if line:
                log.info(
                    "codegraph_kernel_stderr",
                    extra={"msg": "Kernel 内部日志", "data": {"line": line[:2000]}},
                )

    def _start_health_check(self) -> None:
        """启动周期健康检查线程（kernel.ping + 进程存活探测）。"""
        self._stop_health.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop, name="workspace_event-kernel-health", daemon=True
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        """周期 ping；进程退出或无响应则进入 degraded 并退避重启。"""
        while not self._stop_health.wait(_HEALTH_CHECK_INTERVAL):
            proc = self._proc
            if proc is None or proc.poll() is not None:
                log.warning(
                    "codegraph_kernel_exited",
                    extra={"msg": "Kernel 进程意外退出，进入 degraded 并重试"},
                )
                self._handle_crash()
                return
            try:
                self.get_client().ping()
            except CodeGraphKernelError as exc:
                log.warning(
                    "codegraph_kernel_unhealthy",
                    extra={"msg": "Kernel 健康检查失败", "data": {"error": str(exc)}},
                )
                self._handle_crash()
                return

    def _handle_crash(self) -> None:
        """进程崩溃/握手失败的统一处理：清理资源 + 退避重启或置 failed。

        重启在独立 daemon 线程中进行，避免在 health 线程内直接递归 ``start``
        导致 ``_join_threads`` 对自身线程 ``join``（抛 RuntimeError）与死锁。

        退避计数 ``_restart_attempts`` 的读-判-写在 ``_state_lock`` 下原子完成，
        避免 health 线程与 ``start`` 握手失败路径并发调用时超发重启线程（绕过
        退避上限）。注意不在此锁内调用 ``_set_state``（其内部亦取同锁，会死锁），
        故先取锁决定目标状态与退避值，释放后再设状态、起线程。
        """
        self._cleanup_proc()
        with self._state_lock:
            if self._restart_attempts < len(_BACKOFF_SECONDS):
                backoff = _BACKOFF_SECONDS[self._restart_attempts]
                self._restart_attempts += 1
                target_state = KernelState.RESTARTING
            else:
                backoff = None
                target_state = KernelState.FAILED
        if backoff is not None:
            self._set_state(target_state)
            log.info(
                "codegraph_kernel_restart",
                extra={"msg": "退避后重启 Kernel", "data": {"backoff_seconds": backoff}},
            )
            threading.Thread(
                target=self._restart_after_backoff,
                args=(backoff,),
                name="workspace_event-kernel-restart",
                daemon=True,
            ).start()
        else:
            self._set_state(target_state)
            log.error("codegraph_kernel_failed", extra={"msg": "Kernel 重启耗尽退避，置 failed"})

    def _restart_after_backoff(self, backoff: float) -> None:
        """退避后重新拉起 Kernel（运行于独立 restart 线程）。"""
        time.sleep(backoff)
        try:
            self.start()
        except CodeGraphKernelError as exc:
            log.error(
                "codegraph_kernel_restart_failed",
                extra={"msg": "Kernel 重启失败", "data": {"error": str(exc)}},
            )

    def _cleanup_proc(self) -> None:
        """停止 health/stderr 线程并强杀残留子进程。"""
        self._stop_health.set()
        if self._client is not None:
            self._client.stop()
            self._client = None
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._force_kill(proc)
        self._join_threads()
        self._proc = None

    def _force_kill(self, proc: subprocess.Popen) -> None:
        """两轮强杀：terminate 礼貌退出 → 仍存活则 kill 强杀。"""
        try:
            proc.terminate()
            proc.wait(1)
            if proc.poll() is None:
                proc.kill()
                proc.wait(1)
        except OSError:
            pass

    def _join_threads(self) -> None:
        """等待 stderr/health 线程退出（daemon，最多等 2s），跳过自身线程。"""
        current = threading.current_thread()
        for thr in (self._stderr_thread, self._health_thread):
            if thr is not None and thr.is_alive() and thr is not current:
                thr.join(timeout=2.0)
        self._stderr_thread = None
        self._health_thread = None

    def _set_state(self, state: KernelState) -> None:
        """线程安全地设置进程状态。"""
        with self._state_lock:
            self._state = state


# ----------------------------------------------------------------------
# 进程级单例装配（对齐 app.config.configuration 的 set/get 范式）
# ----------------------------------------------------------------------

_SUPERVISOR: CodeGraphKernelSupervisor | None = None


def set_kernel_supervisor(supervisor: CodeGraphKernelSupervisor) -> None:
    """设置进程级 Kernel Supervisor 单例。"""
    global _SUPERVISOR
    _SUPERVISOR = supervisor


def get_kernel_supervisor() -> CodeGraphKernelSupervisor:
    """返回进程级 Kernel Supervisor 单例。

    异常:
        RuntimeError: 未初始化时抛出。
    """
    if _SUPERVISOR is None:
        raise RuntimeError("workspace_event kernel supervisor has not been initialized")
    return _SUPERVISOR
