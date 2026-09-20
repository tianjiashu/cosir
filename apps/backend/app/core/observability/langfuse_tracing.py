"""Langfuse 可观测性接入（LLM/工具调用 trace 的耦合唯一收口）。

本模块是 Langfuse 三方依赖在后端代码中出现的唯一位置（除 ``langfuse_tool_trace_recorder``）：
负责读 ``Settings``、惰性初始化进程级 Langfuse 客户端、提供 turn 级根 observation 上下文管理器
与 LangChain ``CallbackHandler`` 工厂、进程退出 flush。

分层约束：
- 本模块位于 ``core/observability``（core 层），``core → service/config/trace_infra`` 方向合法。
- 所有 langfuse import 均为函数/方法内惰性加载；未启用 / 缺密钥 / 未安装时，运行时行为与
  集成前完全一致，绝不因可观测性失败中断 turn 执行。

Langfuse 客户端 API 版本：基于 langfuse v4（OpenTelemetry 后端）。关键差异（与早期 v2/v3 不同）：
- 客户端构造用 ``Langfuse(public_key=, secret_key=, base_url=)``
  （注意是 ``base_url``，非 ``host``）。
    - 创建 observation 用 ``start_as_current_observation(as_type="span"|"tool"|
      "generation", ...)``，该上下文管理器退出时**自动结束** span，无需手动 ``span.end()``。
- trace 级属性（``session_id`` / ``user_id`` / ``tags`` / ``metadata``）经顶层
  ``propagate_attributes(...)`` 上下文管理器设置，自动传播到其内创建的所有子 span。
- ``CallbackHandler(public_key=)`` 仅按 public_key 复用已初始化的全局 client，因此必须先调用
  ``Langfuse(...)`` 完成全局配置，再创建 ``CallbackHandler``。
"""

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.observability.langfuse_payload_limits import limit_langfuse_payload

_TRACING_WARNING_EVENTS: set[str] = set()


@dataclass(frozen=True)
class TraceMetadata:
    """一次 turn 的可观测元数据（session 分组与检索键）。

    职责：携带把本次 turn 关联到 Langfuse trace / session 所需的稳定标识，不承载业务语义。
    ``session_id=task_id`` 使同一任务多轮 turn 在 Langfuse UI 聚为一个 session；每个 turn
    是 session 内一棵独立 trace。
    """

    task_id: int
    run_id: int
    agent_id: str


@dataclass(frozen=True)
class ConversationRunTraceResult:
    """``conversation_run_trace`` 上下文管理器的产出结果。

    同时携带注入 workflow 的 LangChain callbacks 与本 turn 根 observation 的实际 Langfuse trace_id。
    未启用 tracing 时 ``callbacks`` 为空列表、``trace_id`` 为 None。
    """

    callbacks: list[Any] = field(default_factory=list)
    trace_id: str | None = None


def tracing_enabled() -> bool:
    """判断是否启用 Langfuse trace。

    三条件同时满足才返回 True：① 显式开启 ``LANGFUSE_ENABLED``；② public/secret key 齐备；
    ③ ``langfuse`` 包可导入。任一不满足记一次 warning 并降级。

    参数:
        无。

    返回:
        是否应启用 trace（True 表示可安全创建 Langfuse 客户端与 observation）。

    异常:
        无。

    副作用:
        缺失密钥或未安装时记 ``log.warning``（不重复轰炸）。
    """

    if not Settings.LANGFUSE_ENABLED:
        return False
    if not Settings.LANGFUSE_PUBLIC_KEY or not Settings.LANGFUSE_SECRET_KEY:
        _warn_once(
            "langfuse_disabled_missing_keys",
            "Langfuse 已开启但缺失 public/secret key，降级为不追踪",
            {"base_url": Settings.LANGFUSE_BASE_URL},
        )
        return False
    try:
        import langfuse  # noqa: F401
    except ImportError:
        _warn_once(
            "langfuse_not_installed",
            "Langfuse 已开启但未安装 langfuse 包，降级为不追踪",
            {"base_url": Settings.LANGFUSE_BASE_URL},
        )
        return False
    return True


def _warn_once(event: str, msg: str, data: dict[str, Any] | None = None) -> None:
    """Log a Langfuse degradation warning once per process.

    参数:
        event: 稳定日志事件名。
        msg: 面向人的中文说明。
        data: 可选结构化定位字段，不得包含 secret 原文。

    返回:
        无。

    异常:
        无。

    副作用:
        首次遇到指定事件时写一条 warning 日志。
    """

    if event in _TRACING_WARNING_EVENTS:
        return
    _TRACING_WARNING_EVENTS.add(event)
    log.warning(event, extra={"msg": msg, "data": data or {}})


def _build_langfuse_client() -> Any:
    """按 ``Settings`` 惰性构造（或取单例）Langfuse 客户端。

    langfuse v4 以 ``public_key`` 为进程级单例键；重复调用返回同一配置实例。必须显式传入
    ``base_url``（对应 ``LANGFUSE_BASE_URL``），因为本项目环境变量前缀为 ``CODING_AGENT_``，
    不会被 langfuse 默认环境变量读取逻辑识别。

    参数:
        无。

    返回:
        已配置好 public/secret/base_url 的 Langfuse 客户端实例。

    异常:
        ImportError / LangfuseError: 由调用方（``conversation_run_trace`` / recorder）捕获并降级。

    副作用:
        首次调用时在进程内注册该 public_key 对应的全局 client 单例。
    """

    from langfuse import Langfuse

    return Langfuse(
        public_key=Settings.LANGFUSE_PUBLIC_KEY,
        secret_key=Settings.LANGFUSE_SECRET_KEY,
        base_url=Settings.LANGFUSE_BASE_URL,
        mask=_limit_langfuse_data,
        mask_otel_spans=_limit_langfuse_otel_spans,
    )


def _limit_langfuse_data(*, data: Any, **kwargs: Any) -> Any:
    """Bound payload text set through Langfuse SDK APIs.

    参数:
        data: Langfuse SDK 传入的 input/output/metadata 数据。
        **kwargs: Langfuse SDK 未来可能传入的上下文字段。

    返回:
        字符串受长度限制的数据；内容保持原样。

    异常:
        无。长度处理失败时返回原始数据，避免异常影响 SDK 主流程。

    副作用:
        无。
    """

    try:
        return limit_langfuse_payload(data)
    except Exception:
        log.exception(
            "langfuse_payload_limit_failed",
            extra={"msg": "Langfuse payload 长度限制失败，保留原始数据"},
        )
        return data


def _limit_langfuse_otel_spans(*, params: Any) -> Any:
    """Bound OpenTelemetry span attribute text before Langfuse exports it.

    参数:
        params: Langfuse SDK 传入的 ``MaskOtelSpansParams``。

    返回:
        ``MaskOtelSpansResult`` 或 ``None``。

    异常:
        无。长度处理失败时返回 ``None``，避免阻断 OTel 导出线程。

    副作用:
        无。
    """

    try:
        from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

        patches: dict[Any, Any] = {}
        for identifier, span in getattr(params, "spans", {}).items():
            replacements: dict[str, str | bool | int | float | list[str]] = {}
            for key, value in getattr(span, "attributes", {}).items():
                if isinstance(value, str):
                    limited = limit_langfuse_payload(value)
                    if isinstance(limited, str) and limited != value:
                        replacements[key] = limited
                elif isinstance(value, int | float | bool):
                    continue
                elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
                    limited_sequence = limit_langfuse_payload(list(value))
                    if limited_sequence != value and all(
                        isinstance(item, str) for item in limited_sequence
                    ):
                        replacements[key] = limited_sequence
            if replacements:
                patches[identifier] = OtelSpanPatch(set_attributes=replacements)
        if not patches:
            return None
        return MaskOtelSpansResult(span_patches=patches)
    except Exception:
        log.exception(
            "langfuse_otel_payload_limit_failed",
            extra={"msg": "Langfuse OTel span 长度限制失败，跳过本批次处理"},
        )
        return None


@contextmanager
def conversation_run_trace(metadata: TraceMetadata) -> Iterator[ConversationRunTraceResult]:
    """打开 turn 级根 observation，并产出待注入 workflow 的 LangChain callbacks 列表与 trace_id。

    未启用 → yield ``ConversationRunTraceResult([], None)``，完全空操作（零开销路径）。
    启用 → 构造 Langfuse 客户端，经 ``start_as_current_observation(as_type="span")`` 建立
    turn 根 observation（该 span 由 OTel 自动生成 trace_id 并成为 current context），用
    ``propagate_attributes`` 写 trace 级属性（session/user/tags/metadata），再在该上下文内
    构造 ``CallbackHandler``——不传 ``trace_context``，使 LLM generation observation 经 OTel
    current context 自然传播自动挂到该根下（同一 trace），随后 yield
    ``ConversationRunTraceResult([handler], root_span.trace_id)``。任何 Langfuse 侧异常 →
    ``log.exception`` 后降级为 yield ``ConversationRunTraceResult([], None)``，绝不中断 turn 执行。

    参数:
        metadata: 本次 turn 的可观测元数据（task/turn/agent/workspace 标识）。

    生成:
        ``ConversationRunTraceResult``：callbacks 列表与本 turn 根 observation 的实际 Langfuse
        trace_id（未启用或根 observation 无 trace_id 时为 None；后者同时记 warning 以便排查）。

    异常:
        不向上抛出：Langfuse 客户端/observation 异常被内部捕获并记日志。

    副作用:
        创建 Langfuse 客户端与根 observation、写 trace 属性、构造 CallbackHandler；
        退出上下文时自动结束根 observation。单次对话不主动 flush，避免观测系统网络
        重试阻塞对话终态收口。
    """

    if not tracing_enabled():
        yield ConversationRunTraceResult(callbacks=[], trace_id=None)
        return

    # 仅包裹「客户端构造 + 根 observation 上下文构造」阶段；进入 context 和 handler 构造
    # 需要独立清理已进入的上下文，避免半初始化的 Langfuse 状态泄漏。
    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        client = _build_langfuse_client()
        trace_metadata: dict[str, str] = {
            "task_id": str(metadata.task_id),
            "run_id": str(metadata.run_id),
        }
        root_span_cm = client.start_as_current_observation(
            as_type="span", name=f"turn {metadata.run_id}"
        )
        attr_cm = propagate_attributes(
            session_id=str(metadata.task_id),
            user_id=metadata.agent_id,
            tags=["coding-agent"],
            metadata=trace_metadata,
        )
    except Exception:
        log.exception(
            "langfuse_conversation_run_trace_failed",
            extra={
                "msg": "Langfuse turn trace 初始化失败，降级为不追踪",
                "data": {"run_id": metadata.run_id, "task_id": metadata.task_id},
            },
        )
        yield ConversationRunTraceResult(callbacks=[], trace_id=None)
        return

    root_entered = False
    attr_entered = False
    try:
        root_span = root_span_cm.__enter__()
        root_entered = True
        attr_cm.__enter__()
        attr_entered = True
        handler = CallbackHandler(
            public_key=Settings.LANGFUSE_PUBLIC_KEY,
        )
    except Exception:
        log.exception(
            "langfuse_conversation_run_trace_failed",
            extra={
                "msg": "Langfuse 根 observation 或 CallbackHandler 初始化失败，降级为不追踪",
                "data": {"run_id": metadata.run_id, "task_id": metadata.task_id},
            },
        )
        _safe_exit_langfuse_context(
            attr_cm if attr_entered else None,
            "langfuse_attributes_exit_failed",
            metadata,
        )
        _safe_exit_langfuse_context(
            root_span_cm if root_entered else None,
            "langfuse_root_span_exit_failed",
            metadata,
        )
        yield ConversationRunTraceResult(callbacks=[], trace_id=None)
        return

    try:
        root_trace_id = getattr(root_span, "trace_id", None)
        # Langfuse SDK 会在后台批量上报。这里不能调用同步 client.flush()：
        # flush 的网络重试属于非关键观测路径，不能阻塞 ConversationRun。
        yield ConversationRunTraceResult(
            callbacks=[handler],
            trace_id=root_trace_id,
        )
    finally:
        exc_info = sys.exc_info()
        _safe_exit_langfuse_context(
            attr_cm,
            "langfuse_attributes_exit_failed",
            metadata,
            exc_info,
        )
        _safe_exit_langfuse_context(
            root_span_cm,
            "langfuse_root_span_exit_failed",
            metadata,
            exc_info,
        )


def _safe_exit_langfuse_context(
    context_manager: Any | None,
    event: str,
    metadata: TraceMetadata,
    exc_info: tuple[type[BaseException] | None, BaseException | None, Any] | None = None,
) -> None:
    """Best-effort exit a Langfuse context manager.

    参数:
        context_manager: 已进入的 Langfuse context manager；为 ``None`` 时跳过。
        event: 退出失败时使用的稳定日志事件名。
        metadata: 当前 turn 的定位元数据。
        exc_info: 可选的原始业务异常上下文，用于让 Langfuse 正确标记失败 observation。

    返回:
        无。

    异常:
        无。退出异常被记录后吞掉。

    副作用:
        可能结束 Langfuse observation；失败时写 error 日志。
    """

    if context_manager is None:
        return
    try:
        context_manager.__exit__(*(exc_info or (None, None, None)))
    except Exception:
        log.exception(
            event,
            extra={
                "msg": "Langfuse context 退出失败，已忽略以避免影响 turn",
                "data": {"run_id": metadata.run_id, "task_id": metadata.task_id},
            },
        )


def flush_langfuse() -> None:
    """尽力 flush 缓冲的 Langfuse trace。

    进程退出前调用（``app/lifespan.py`` 关闭编排）。未启用或客户端创建/上报失败时
    仅记日志，不抛出（绝不因可观测性失败影响主流程关闭）。

    参数:
        无。

    返回:
        无。

    异常:
        不向上抛出：Langfuse 侧异常被内部捕获并记日志。

    副作用:
        触发 Langfuse 客户端后台批量上报；可能创建临时客户端实例。
    """

    if not tracing_enabled():
        return
    try:
        client = _build_langfuse_client()
        client.flush()
    except Exception:
        log.exception(
            "langfuse_flush_failed",
            extra={"msg": "Langfuse 进程退出 flush 失败（忽略）"},
        )
