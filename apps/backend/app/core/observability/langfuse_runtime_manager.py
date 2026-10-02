"""Langfuse 运行时生命周期管理器。

协调 Langfuse client 与单个 Agent Run 观测上下文的进程级管理器。配置变更时旧 client 先进入
draining；已有 Run 继续使用旧 client，直到 lease 归零后执行 flush、shutdown 和 SDK 资源清理。
draining 期间新 Run 不会错误复用旧配置，而是异步等待旧 client 彻底关闭，随后按新配置懒加载
新 client。等待过程可被 Run 取消，不会阻塞事件循环。

本模块不负责持久化 trace_id，也不负责发 Transport 事件；这些事实由 Run service 层处理。
所有 Langfuse 异常都记录日志并降级，不得改变 Agent 主流程结果。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from app.config.logging.logger import log
from app.core.observability.langfuse_config import LangfuseConfig, TraceMetadata
from app.core.observability.langfuse_sdk import LangfuseSdk, _LangfuseSdkAdapter
from app.core.observability.tool_trace_recorder import (
    ToolTraceRecorder,
    _NullToolTraceRecorder,
)


@dataclass(frozen=True, slots=True)
class ConversationRunTraceResult:
    """统一根 Trace 上下文产出的 callbacks、trace_id 和工具 recorder。

    当任意 Langfuse 初始化阶段失败时，三个字段会整体降级为空实现，避免出现只有工具
    Trace 没有根 Trace 的孤儿观测链路。
    """

    callbacks: list[Any] = field(default_factory=list)
    trace_id: str | None = None
    tool_trace_recorder: ToolTraceRecorder = field(default_factory=_NullToolTraceRecorder)


@dataclass
class _ClientEntry:
    """一个 client 及其运行期 lease 计数。"""

    config: LangfuseConfig
    client: Any
    active_runs: int = 0
    draining: bool = False
    closed: bool = False
    flush_attempted: bool = False


class LangfuseRuntimeManager:
    """协调 Langfuse client 与单个 Agent Run 观测上下文的进程级管理器。

    配置变更时旧 client 先进入 draining；已有 Run 继续使用旧 client，直到 lease 归零后
    执行 flush、shutdown 和 SDK 资源清理。draining 期间新 Run 不会错误复用旧配置，而是
    异步等待旧 client 彻底关闭，随后按新配置懒加载新 client。等待过程可被 Run 取消，
    不会阻塞事件循环。

    本类不负责持久化 trace_id，也不负责发 Transport 事件；这些事实由 Run service 层处理。
    所有 Langfuse 异常都记录日志并降级，不得改变 Agent 主流程结果。
    """

    def __init__(
        self,
        *,
        sdk: LangfuseSdk | None = None,
        tool_recorder_factory: Callable[[Any], ToolTraceRecorder] | None = None,
    ) -> None:
        """创建一个独立的进程内 Langfuse 生命周期管理器。"""

        from app.core.observability.langfuse_tool_trace_recorder import (
            LangfuseToolTraceRecorder,
        )

        self._sdk = sdk or _LangfuseSdkAdapter()
        self._tool_recorder_factory = tool_recorder_factory or LangfuseToolTraceRecorder
        self._lock = RLock()
        self._config: LangfuseConfig | None = None
        self._current: _ClientEntry | None = None
        self._retired: list[_ClientEntry] = []
        self._closed = False

    def reload(self, config: LangfuseConfig) -> None:
        """原子替换后续 Run 使用的配置，并安全收敛旧 client。

        参数:
            config: 新的不可变配置快照。

        副作用:
            配置变化时旧 client 进入 draining；没有 active Run 时立即 flush/shutdown，
            有 active Run 时延迟到最后一个 lease 释放。方法本身不创建新 client。

        异常:
            不向上抛出 Langfuse flush/shutdown 异常；配置切换仍会完成。
        """

        with self._lock:
            if self._config is not None and self._config.fingerprint() == config.fingerprint():
                self._closed = False
                return
            self._config = config
            self._closed = False
            old_entry = self._current
            self._current = None
            if old_entry is None:
                return
            old_entry.draining = True
            if old_entry not in self._retired:
                self._retired.append(old_entry)
            if old_entry.active_runs == 0:
                self._close_entry_locked(old_entry)

    def reload_from_settings(self) -> None:
        """从当前 ``Settings`` 重载 Langfuse 配置。"""

        self.reload(LangfuseConfig.from_settings())

    def tracing_enabled(self) -> bool:
        """返回当前配置是否具备创建 Langfuse 观测的条件。"""

        with self._lock:
            config = self._config or LangfuseConfig.from_settings()
            if self._config is None:
                self._config = config
            return config.is_usable(self._sdk)

    @asynccontextmanager
    async def conversation_run_trace(
        self,
        metadata: TraceMetadata,
    ) -> AsyncIterator[ConversationRunTraceResult]:
        """异步原子创建并管理一个 Run 的根 Trace、callbacks 和工具 recorder。

        配置热更新正在收敛旧 client 时，本方法异步等待旧 Run 释放 lease；不会阻塞事件
        循环，也不会把新 Run 静默降级成空 Trace。管理器关闭或配置不可用时才返回空观测。
        """

        entry = await self._acquire_entry()
        if entry is None:
            yield ConversationRunTraceResult()
            return

        root_context: Any | None = None
        attributes_context: Any | None = None
        root_entered = False
        attributes_entered = False
        try:
            trace_metadata = {
                "task_id": str(metadata.task_id),
                "run_id": str(metadata.run_id),
            }
            root_context = entry.client.start_as_current_observation(
                as_type="span",
                name=f"turn {metadata.run_id}",
            )
            attributes_context = self._sdk.propagate_attributes(
                session_id=str(metadata.task_id),
                user_id=metadata.agent_id,
                tags=["coding-agent"],
                metadata=trace_metadata,
            )
            root_span = root_context.__enter__()
            root_entered = True
            attributes_context.__enter__()
            attributes_entered = True
            public_key = entry.config.public_key
            if public_key is None:
                raise RuntimeError("Langfuse public key disappeared from an active config")
            handler = self._sdk.build_callback_handler(public_key)
            recorder = self._tool_recorder_factory(entry.client)
            result = ConversationRunTraceResult(
                callbacks=[handler],
                trace_id=getattr(root_span, "trace_id", None),
                tool_trace_recorder=recorder,
            )
        except Exception:
            log.exception(
                "langfuse_run_trace_init_failed",
                extra={
                    "msg": "Langfuse 根 Trace 与工具 Trace 原子初始化失败，整体降级",
                    "data": {"run_id": metadata.run_id, "task_id": metadata.task_id},
                },
            )
            _safe_exit_context(attributes_context if attributes_entered else None, metadata)
            _safe_exit_context(root_context if root_entered else None, metadata)
            self._release_entry(entry)
            yield ConversationRunTraceResult()
            return

        try:
            yield result
        finally:
            exc_info = sys.exc_info()
            _safe_exit_context(attributes_context, metadata, exc_info)
            _safe_exit_context(root_context, metadata, exc_info)
            self._release_entry(entry)

    def shutdown(self) -> None:
        """关闭管理器并 flush 所有当前及已退休 client。

        方法幂等，且不检查当前 ``enabled`` 配置；即使 Langfuse 已被热关闭，也会处理已经
        创建的 client。关闭失败只记日志，不阻断后端生命周期收口。
        """

        with self._lock:
            self._closed = True
            entries = [entry for entry in [self._current, *self._retired] if entry is not None]
            self._current = None
            self._retired.clear()
            for entry in entries:
                if entry.closed:
                    continue
                entry.draining = True
                self._flush_entry_locked(entry)
                if entry.active_runs == 0:
                    self._close_entry_locked(entry)

    async def _acquire_entry(self) -> _ClientEntry | None:
        """异步获取当前配置的 Run lease，并等待旧 client 完成收敛。"""

        waiting_logged = False
        while True:
            with self._lock:
                if self._closed:
                    return None
                if self._retired:
                    if not waiting_logged:
                        log.info(
                            "langfuse_reconfigure_waiting",
                            extra={"msg": "Langfuse 旧 client 尚未收敛，本轮等待后使用新配置"},
                        )
                        waiting_logged = True
                else:
                    config = self._config or LangfuseConfig.from_settings()
                    self._config = config
                    if not config.is_usable(self._sdk):
                        return None
                    if self._current is None:
                        try:
                            self._current = _ClientEntry(
                                config=config, client=self._sdk.build_client(config)
                            )
                        except Exception:
                            log.exception(
                                "langfuse_client_init_failed",
                                extra={"msg": "Langfuse client 初始化失败，降级为不追踪"},
                            )
                            return None
                    self._current.active_runs += 1
                    return self._current
            await asyncio.sleep(0.01)

    def _release_entry(self, entry: _ClientEntry) -> None:
        """释放 Run lease，并在 draining client 无引用后完成关闭。"""

        with self._lock:
            entry.active_runs = max(0, entry.active_runs - 1)
            if entry.draining and entry.active_runs == 0:
                self._close_entry_locked(entry)

    def _close_entry_locked(self, entry: _ClientEntry) -> None:
        """在生命周期锁内一次性 flush、shutdown 并清理 SDK 资源。"""

        if entry.closed:
            return
        entry.closed = True
        if entry in self._retired:
            self._retired.remove(entry)
        self._flush_entry_locked(entry)
        try:
            entry.client.shutdown()
        except Exception:
            log.exception(
                "langfuse_client_shutdown_failed", extra={"msg": "Langfuse client shutdown 失败"}
            )
        try:
            public_key = entry.config.public_key
            if public_key:
                self._sdk.reset_resource(public_key)
        except Exception:
            log.exception(
                "langfuse_resource_reset_failed",
                extra={"msg": "Langfuse SDK 资源清理失败，已忽略"},
            )

    def _flush_entry_locked(self, entry: _ClientEntry) -> None:
        """最多执行一次 client flush，允许 shutdown 先行发起 flush。"""

        if entry.flush_attempted:
            return
        entry.flush_attempted = True
        try:
            entry.client.flush()
        except Exception:
            log.exception(
                "langfuse_client_flush_failed", extra={"msg": "Langfuse client flush 失败"}
            )


def _safe_exit_context(
    context_manager: Any | None,
    metadata: TraceMetadata,
    exc_info: tuple[type[BaseException] | None, BaseException | None, Any] | None = None,
) -> None:
    """尽力退出 Langfuse 上下文，避免观测关闭异常影响 Agent Run。"""

    if context_manager is None:
        return
    try:
        context_manager.__exit__(*(exc_info or (None, None, None)))
    except Exception:
        log.exception(
            "langfuse_context_exit_failed",
            extra={
                "msg": "Langfuse 上下文退出失败，已忽略",
                "data": {"run_id": metadata.run_id, "task_id": metadata.task_id},
            },
        )
