"""Workspace 索引生命周期编排服务。

单一职责：把 ``status`` 结果编排为 ``init``/``sync`` 动作，并把失败归一化为降级结果。
确保 workspace 在 Agent 使用前索引就绪。

职责边界：
- 负责：status → init/sync 编排、按 workspace 的并发去重（组合 InflightRegistry）、
        失败降级判定与结构化日志。
- 不负责：Kernel 进程管理（归 Supervisor）、RPC 细节（归 Client）、
        索引算法（归上游 CodeGraph）、task 状态机与事件发布（归 TaskService，第二阶段另文）。
"""

from app.codegraph import (
    CodeGraphKernelClient,
    CodeGraphKernelError,
    CodeGraphKernelUnavailableError,
)
from app.config.logging.logger import log
from app.config.settings import Settings
from app.models.workspace_index_readiness import WorkspaceIndexReadiness
from app.utils.inflight_registry import InflightRegistry


class CodeGraphLifecycleService:
    """Workspace 索引生命周期编排：确保 workspace 在 Agent 使用前索引就绪。

    ``ensure_ready`` 是同步阻塞语义（可放进线程/任务等待），绝不抛异常——
    失败一律转为 ``WorkspaceIndexReadiness(ready=False)``，由调用方决定降级。
    """

    def __init__(
        self,
        client: CodeGraphKernelClient,
        inflight: InflightRegistry | None = None,
    ) -> None:
        """构造生命周期编排服务。

        参数:
            client: 已就绪的 Kernel RPC 客户端。
            inflight: 可选的并发去重注册表；省略时新建（供测试注入）。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self._client = client
        self._inflight = inflight if inflight is not None else InflightRegistry()

    def ensure_ready(self, workspace_path: str) -> WorkspaceIndexReadiness:
        """确保 workspace 索引就绪（阻塞至完成），失败以降级结果返回。

        参数:
            workspace_path: 目标 workspace 的绝对路径。

        返回:
            WorkspaceIndexReadiness：ready=True 表示 CodeGraph 可用；
            ready=False 表示降级到文件搜索（reason 含英文原因）。

        异常:
            无（所有失败归一化为降级结果返回，不抛）。

        副作用:
            可能触发 init/sync 写入型索引动作；同 workspace 并发只执行一次。
        """
        key = self._normalize_key(workspace_path)
        return self._inflight.run(key, lambda: self._prepare(workspace_path))

    # ------------------------------------------------------------------
    # Internal: orchestration
    # ------------------------------------------------------------------

    def _prepare(self, workspace_path: str) -> WorkspaceIndexReadiness:
        """执行一次完整的索引准备编排（singleflight 内运行）。"""
        started = self._monotonic_ms()
        log.info(
            "codegraph_workspace_preparing",
            extra={"msg": "开始准备 workspace 索引", "data": {"workspace_path": workspace_path}},
        )
        try:
            status = self._client.index_status(workspace_path)
        except CodeGraphKernelUnavailableError as exc:
            return self._degraded(
                workspace_path, "unavailable", f"kernel unavailable: {exc}", started
            )
        except CodeGraphKernelError as exc:
            return self._degraded(
                workspace_path, "unavailable", f"status query failed: {exc}", started
            )
        except (KeyError, TypeError, ValueError) as exc:
            # 与第二个 try 的兜底对称：即便 status 解析逃逸裸异常也归一化降级，
            # 严格兑现「ensure_ready 绝不抛异常」。
            return self._degraded(
                workspace_path, "unavailable", f"malformed status payload: {exc}", started
            )

        try:
            if status.state == "unindexed":
                return self._do_init(workspace_path, started)
            if status.state == "ready":
                return self._do_sync(workspace_path, started)
            if status.state == "indexing":
                return self._degraded(
                    workspace_path,
                    "failed",
                    "index is being built by another process",
                    started,
                )
            # state == "failed"
            return self._degraded(
                workspace_path,
                "failed",
                "index is in failed state; rebuild required",
                started,
            )
        except CodeGraphKernelError as exc:
            # init/sync 的 Kernel 级失败（含超时、进程退出）统一降级。
            return self._degraded(
                workspace_path, "unavailable", f"index action failed: {exc}", started
            )
        except (KeyError, TypeError, ValueError) as exc:
            # 防御兜底：即便 client 解析意外逃逸畸形 payload 的裸异常，也归一化降级，
            # 确保「ensure_ready 绝不抛异常」不依赖 client 的解析严谨性。
            return self._degraded(
                workspace_path, "unavailable", f"malformed index payload: {exc}", started
            )

    def _do_init(self, workspace_path: str, started: int) -> WorkspaceIndexReadiness:
        """执行首次索引（codegraph_init，长超时）。"""
        timeout = Settings.CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS
        log.info(
            "codegraph_indexing",
            extra={"msg": "开始首次建索引", "data": {"workspace_path": workspace_path}},
        )
        result = self._client.index_init(workspace_path, timeout=timeout)
        return self._ready(
            workspace_path,
            "init",
            result.files_indexed,
            self._elapsed_ms(started),
        )

    def _do_sync(self, workspace_path: str, started: int) -> WorkspaceIndexReadiness:
        """执行增量同步（codegraph_sync，D2：已索引也总是显式 sync）。"""
        timeout = Settings.CODEGRAPH_INDEX_SYNC_TIMEOUT_SECONDS
        log.info(
            "codegraph_syncing",
            extra={"msg": "开始增量同步索引", "data": {"workspace_path": workspace_path}},
        )
        result = self._client.index_sync(workspace_path, timeout=timeout)
        files_changed = result.files_added + result.files_modified + result.files_removed
        return self._ready(
            workspace_path,
            "sync",
            files_changed,
            self._elapsed_ms(started),
        )

    def _ready(
        self,
        workspace_path: str,
        action: str,
        files_changed: int,
        duration_ms: int,
    ) -> WorkspaceIndexReadiness:
        """构造就绪结果并写 ready 日志。"""
        readiness = WorkspaceIndexReadiness(
            ready=True,
            state="ready",
            action_taken=action,
            files_changed=files_changed,
            duration_ms=duration_ms,
            degraded_reason=None,
        )
        log.info(
            "codegraph_workspace_ready",
            extra={
                "msg": "workspace 索引就绪",
                "data": {
                    "workspace_path": workspace_path,
                    "action": action,
                    "files_changed": files_changed,
                    "duration_ms": duration_ms,
                },
            },
        )
        return readiness

    def _degraded(
        self,
        workspace_path: str,
        state: str,
        reason: str,
        started: int,
    ) -> WorkspaceIndexReadiness:
        """构造降级结果并写 degraded 日志（warning）。"""
        readiness = WorkspaceIndexReadiness(
            ready=False,
            state=state,
            action_taken="none",
            files_changed=0,
            duration_ms=self._elapsed_ms(started),
            degraded_reason=reason,
        )
        log.warning(
            "codegraph_workspace_degraded",
            extra={
                "msg": "workspace 索引不可用，降级到文件搜索",
                "data": {
                    "workspace_path": workspace_path,
                    "state": state,
                    "degraded_reason": reason,
                    "duration_ms": readiness.duration_ms,
                },
            },
        )
        return readiness

    @staticmethod
    def _normalize_key(workspace_path: str) -> str:
        """规范化 InflightRegistry 去重键（绝对路径 + 大小写不敏感）。

        把 workspace 路径归一化为「可作去重键」的稳定形式：先 ``realpath`` 解析
        符号链接/相对路径得到绝对路径，再 ``normcase`` 做平台相关的大小写归一
        （Windows 下路径大小写不敏感，保证同一目录不同大小写写法命中同一 key，
        避免对同一 workspace 并发触发两次索引）。

        参数:
            workspace_path: 待归一化的 workspace 路径。

        返回:
            归一化后的去重键（绝对路径，Windows 下统一小写形式）。

        异常:
            无（``os.path.realpath`` 对不存在的路径可能抛 ``OSError``，此处捕获并
                回退到 ``os.path.abspath``）。

        副作用:
            无。
        """
        import os

        try:
            resolved = os.path.realpath(workspace_path)
        except OSError:
            # Windows 下 ``os.path.realpath`` 对不存在的路径行为与 POSIX 有差异
            # （可能不展开符号链接或对非法路径抛错），失败时回退到 ``abspath``。
            resolved = os.path.abspath(workspace_path)
        return os.path.normcase(resolved)

    @staticmethod
    def _monotonic_ms() -> int:
        import time

        return int(time.monotonic() * 1000)

    @staticmethod
    def _elapsed_ms(started: int) -> int:
        return CodeGraphLifecycleService._monotonic_ms() - started
