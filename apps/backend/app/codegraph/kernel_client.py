"""CodeGraph Kernel 的极薄 JSON-line RPC 客户端。

单一职责：通过子进程 stdin/stdout 与 Kernel 通信。负责 JSON-line 序列化、
``request_id`` 生成与匹配、超时、取消、协议错误码 → Python 异常转换。**不持有
进程生命周期（归 Supervisor）、不解析业务语义（QueryResult 原样上交）。**

并发模型：单 reader 线程消费 stdout，按 ``id`` 把响应投递给等待中的 Future；
stdin 写入由 ``_write_lock`` 串行化避免多请求交错；``_pending`` 的读写由
``_pending_lock`` 保护，避免 reader 线程迭代期间调用线程插入新 key。
"""

import json
import threading
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from subprocess import Popen
from typing import Any

from app.config.logging.logger import log
from app.codegraph.exceptions import (
    CodeGraphKernelError,
    CodeGraphKernelTimeoutError,
    CodeGraphKernelUnavailableError,
    CodeGraphProtocolIncompatibleError,
    error_from_code,
)
from app.codegraph.protocol import (
    METHOD_HELLO,
    METHOD_INDEX_INIT,
    METHOD_INDEX_STATUS,
    METHOD_INDEX_SYNC,
    METHOD_PING,
    METHOD_SHUTDOWN,
    PROTOCOL_VERSION,
    HelloResult,
    IndexInitResult,
    IndexStatusResult,
    IndexSyncResult,
    KernelErrorCode,
    PingResult,
    QueryResult,
)


class CodeGraphKernelClient:
    """Kernel 子进程的 RPC 客户端（不管理进程生命周期）。"""

    def __init__(self, proc: Popen, timeout_seconds: float = 30.0) -> None:
        """构造客户端并启动 stdout 读取线程。

        参数:
            proc: 已启动的 Kernel 子进程（stdin/stdout 须为管道）。
            timeout_seconds: 单次请求默认超时（秒）。

        返回:
            无。

        异常:
            无。

        副作用:
            启动 reader 线程消费 ``proc.stdout``；注册进程退出监听标记。
        """
        self._proc = proc
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, Future[Any]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop_reader = threading.Event()
        self._reader = threading.Thread(
            target=self._read_loop, name="codegraph-kernel-reader", daemon=True
        )
        self._reader.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """发送一次 RPC 请求并等待响应。

        参数:
            method: 方法名（如 ``kernel.hello`` / ``codegraph_explore``）。
            params: 请求参数；查询方法须含 ``workspace_path``。
            timeout: 本次超时（秒）；省略用构造默认值。

        返回:
            成功响应的 ``result`` 字段（HelloResult/PingResult/QueryResult 等，
            由调用方按 method 转型）；查询方法返回 ``QueryResult``。

        异常:
            CodeGraphKernelTimeoutError: 超时未收到响应。
            CodeGraphKernelUnavailableError: 进程已退出或管道断裂。
            CodeGraphKernelError（子类）: 协议层返回 error（经 ``error_from_code`` 映射）。

        副作用:
            写一行 JSON 到 stdin；在读线程投递前阻塞。
        """
        if self._stop_reader.is_set() or self._proc.poll() is not None:
            raise CodeGraphKernelUnavailableError("kernel process is not available for requests")

        wait = timeout if timeout is not None else self._timeout_seconds
        request_id = uuid.uuid4().hex
        future: Future[Any] = Future()
        with self._pending_lock:
            self._pending[request_id] = future
        try:
            self._write_line({"id": request_id, "method": method, "params": params or {}})
            return future.result(timeout=wait)
        except CodeGraphKernelError:
            raise
        except FuturesTimeoutError as exc:
            future.cancel()
            raise CodeGraphKernelTimeoutError(
                f"kernel request {method} timed out after {wait}s"
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def hello(self) -> HelloResult:
        """发起 kernel.hello 握手并校验协议版本兼容。

        对 Kernel 返回做字段显式取值，字段缺失/多余时抛
        ``CodeGraphProtocolIncompatibleError``（归为协议层错误，而非裸 TypeError 逃逸）。
        """
        result = self.call(METHOD_HELLO)
        if not isinstance(result, dict):
            raise CodeGraphProtocolIncompatibleError("kernel.hello returned a non-object payload")
        try:
            hello = HelloResult(
                protocol_version=str(result["protocol_version"]),
                kernel_version=str(result["kernel_version"]),
                codegraph_version=str(result["codegraph_version"]),
                capabilities=[str(c) for c in result["capabilities"]],
                platform=str(result["platform"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeGraphProtocolIncompatibleError(
                f"kernel.hello payload missing/invalid field: {exc}"
            ) from exc
        if hello.protocol_version != PROTOCOL_VERSION:
            raise CodeGraphProtocolIncompatibleError(
                f"kernel protocol {hello.protocol_version} != expected {PROTOCOL_VERSION}"
            )
        return hello

    def ping(self) -> PingResult:
        """发起 kernel.ping 健康检查（显式取值，字段异常归为协议错误）。"""
        result = self.call(METHOD_PING)
        if not isinstance(result, dict):
            raise CodeGraphProtocolIncompatibleError("kernel.ping returned a non-object payload")
        try:
            return PingResult(
                ok=bool(result["ok"]),
                uptime_ms=int(result["uptime_ms"]),
                active_workspaces=int(result["active_workspaces"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeGraphProtocolIncompatibleError(
                f"kernel.ping payload missing/invalid field: {exc}"
            ) from exc

    def query(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = None,
    ) -> QueryResult:
        """发起一次查询工具调用，返回归一化 QueryResult。"""
        result = self.call(method, params, timeout)
        return QueryResult(
            content=result.get("content", []),
            is_error=result.get("is_error", False),
        )

    def index_status(
        self,
        workspace_path: str,
        timeout: float | None = None,
    ) -> IndexStatusResult:
        """查询 workspace 索引状态（codegraph_status）。"""
        result = self.call(METHOD_INDEX_STATUS, {"workspace_path": workspace_path}, timeout)
        return self._index_status_result(result)

    def index_init(
        self,
        workspace_path: str,
        timeout: float | None = None,
    ) -> IndexInitResult:
        """创建 workspace 索引（codegraph_init，写入型，可长超时）。"""
        result = self.call(METHOD_INDEX_INIT, {"workspace_path": workspace_path}, timeout)
        return self._index_init_result(result)

    def index_sync(
        self,
        workspace_path: str,
        timeout: float | None = None,
    ) -> IndexSyncResult:
        """增量同步 workspace 索引（codegraph_sync，写入型）。"""
        result = self.call(METHOD_INDEX_SYNC, {"workspace_path": workspace_path}, timeout)
        return self._index_sync_result(result)

    @staticmethod
    def _index_status_result(result: Any) -> IndexStatusResult:
        """解析 codegraph_status 响应（字段缺失/类型错归为协议错误，对齐 hello 范式）。"""
        if not isinstance(result, dict):
            raise CodeGraphProtocolIncompatibleError(
                "codegraph_status returned a non-object payload"
            )
        try:
            last_indexed = result["last_indexed_at"]
            return IndexStatusResult(
                state=str(result["state"]),
                last_indexed_at=int(last_indexed) if last_indexed is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeGraphProtocolIncompatibleError(
                f"codegraph_status payload missing/invalid field: {exc}"
            ) from exc

    @staticmethod
    def _index_init_result(result: Any) -> IndexInitResult:
        """解析 codegraph_init 响应（畸形 payload 归为协议错误，保证上层不逃逸裸异常）。"""
        if not isinstance(result, dict):
            raise CodeGraphProtocolIncompatibleError(
                "codegraph_init returned a non-object payload"
            )
        try:
            return IndexInitResult(
                state=str(result["state"]),
                files_indexed=int(result["files_indexed"]),
                duration_ms=int(result["duration_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeGraphProtocolIncompatibleError(
                f"codegraph_init payload missing/invalid field: {exc}"
            ) from exc

    @staticmethod
    def _index_sync_result(result: Any) -> IndexSyncResult:
        """解析 codegraph_sync 响应（畸形 payload 归为协议错误，保证上层不逃逸裸异常）。"""
        if not isinstance(result, dict):
            raise CodeGraphProtocolIncompatibleError(
                "codegraph_sync returned a non-object payload"
            )
        try:
            return IndexSyncResult(
                state=str(result["state"]),
                files_added=int(result["files_added"]),
                files_modified=int(result["files_modified"]),
                files_removed=int(result["files_removed"]),
                duration_ms=int(result["duration_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeGraphProtocolIncompatibleError(
                f"codegraph_sync payload missing/invalid field: {exc}"
            ) from exc

    def shutdown(self) -> None:
        """请求 Kernel 优雅关闭（不阻塞等待进程退出）。"""
        try:
            self.call(METHOD_SHUTDOWN)
        except CodeGraphKernelError as exc:
            log.warning(
                "codegraph_kernel_shutdown_failed",
                extra={"msg": "kernel.shutdown 调用失败", "data": {"error": str(exc)}},
            )

    def stop(self) -> None:
        """停止客户端：标记 reader 停止并等待线程退出。"""
        self._stop_reader.set()
        if self._reader.is_alive():
            self._reader.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Internal: IO
    # ------------------------------------------------------------------

    def _write_line(self, obj: dict[str, Any]) -> None:
        """线程安全地写一行 JSON 到 stdin；管道断裂转 Kernel 不可用。"""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            stdin = self._proc.stdin
            if stdin is None:
                raise CodeGraphKernelUnavailableError("kernel stdin is not available")
            try:
                stdin.write(line)
                stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                raise CodeGraphKernelUnavailableError(
                    f"kernel stdin write failed: {exc}"
                ) from exc

    def _read_loop(self) -> None:
        """消费 stdout 的 JSON-line，按 id 投递给等待中的 Future。"""
        stdout = self._proc.stdout
        if stdout is None:
            return
        try:
            for raw in stdout:
                if self._stop_reader.is_set():
                    break
                self._dispatch_line(raw)
        except (ValueError, OSError):
            # 进程退出 / 管道断裂：把所有 pending 标记为不可用（finally 兜底）。
            pass
        finally:
            # 无论正常停止还是管道断裂，统一把所有仍在等待的请求失败化，
            # 避免调用方无限挂起。stop() 后本就不应有新请求。
            self._fail_all_pending()

    def _dispatch_line(self, raw: str) -> None:
        """解析单行并投递；非法 JSON 或缺少 id 的响应被忽略。"""
        line = raw.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        response_id = msg.get("id")
        if not isinstance(response_id, str):
            return
        with self._pending_lock:
            future = self._pending.get(response_id)
            if future is None or future.done():
                return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            try:
                code = KernelErrorCode(err.get("code", "INTERNAL"))
            except ValueError:
                code = KernelErrorCode.INTERNAL
            future.set_exception(
                error_from_code(
                    code,
                    err.get("message", "kernel error"),
                    bool(err.get("retryable", False)),
                )
            )
        else:
            future.set_result(msg.get("result"))

    def _fail_all_pending(self) -> None:
        """进程不可用时把所有等待中的请求标记为 Kernel 不可用（线程安全快照）。"""
        with self._pending_lock:
            snapshots = list(self._pending.items())
        for _request_id, future in snapshots:
            if not future.done():
                future.set_exception(
                    CodeGraphKernelUnavailableError("kernel process exited or pipe broke")
                )
