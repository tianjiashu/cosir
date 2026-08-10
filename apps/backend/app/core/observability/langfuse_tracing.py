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

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.config.logging.logger import log
from app.config.settings import Settings


@dataclass(frozen=True)
class TraceMetadata:
    """一次 turn 的可观测元数据（session 分组与检索键）。

    职责：携带把本次 turn 关联到 Langfuse trace / session 所需的稳定标识，不承载业务语义。
    ``session_id=task_id`` 使同一任务多轮 turn 在 Langfuse UI 聚为一个 session；每个 turn
    是 session 内一棵独立 trace。
    """

    task_id: str
    turn_id: str
    agent_id: str


@dataclass(frozen=True)
class TurnTraceResult:
    """``turn_trace`` 上下文管理器的产出结果。

    同时携带注入 workflow 的 LangChain callbacks 与本 turn 预分配的 Langfuse trace_id。
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
        log.warning(
            "langfuse_disabled_missing_keys",
            extra={
                "msg": "Langfuse 已开启但缺失 public/secret key，降级为不追踪",
                "data": {"base_url": Settings.LANGFUSE_BASE_URL},
            },
        )
        return False
    try:
        import langfuse  # noqa: F401
    except ImportError:
        log.warning(
            "langfuse_not_installed",
            extra={
                "msg": "Langfuse 已开启但未安装 langfuse 包，降级为不追踪",
                "data": {"base_url": Settings.LANGFUSE_BASE_URL},
            },
        )
        return False
    return True


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
        ImportError / LangfuseError: 由调用方（``turn_trace`` / recorder）捕获并降级。

    副作用:
        首次调用时在进程内注册该 public_key 对应的全局 client 单例。
    """

    from langfuse import Langfuse

    return Langfuse(
        public_key=Settings.LANGFUSE_PUBLIC_KEY,
        secret_key=Settings.LANGFUSE_SECRET_KEY,
        base_url=Settings.LANGFUSE_BASE_URL,
    )


@contextmanager
def turn_trace(metadata: TraceMetadata) -> Iterator[TurnTraceResult]:
    """打开 turn 级根 observation，并产出待注入 workflow 的 LangChain callbacks 列表与 trace_id。

    未启用 → yield ``TurnTraceResult([], None)``，完全空操作（零开销路径）。启用 → 构造 Langfuse
    客户端，预分配一个稳定的 ``trace_id`` 并经 ``CallbackHandler(trace_context=...)`` 注入，再经
    ``start_as_current_observation(as_type="span")`` 建立 turn 根 observation（OTel current
    context 自然传播，使 ``CallbackHandler`` 的 generation observation 自动挂到该根下），用
    ``propagate_attributes`` 写 trace 级属性（session/user/tags/metadata），在该上下文内构造
    ``CallbackHandler`` 后 yield ``TurnTraceResult([handler], trace_id)``。任何 Langfuse 侧异常 →
    ``log.exception`` 后降级为 yield ``TurnTraceResult([], None)``，绝不中断 turn 执行。

    参数:
        metadata: 本次 turn 的可观测元数据（task/turn/agent/workspace 标识）。

    生成:
        ``TurnTraceResult``：callbacks 列表与本 turn 预分配的 Langfuse trace_id（未启用时为 None）。

    异常:
        不向上抛出：Langfuse 客户端/observation 异常被内部捕获并记日志。

    副作用:
        创建 Langfuse 客户端与根 observation、写 trace 属性、构造 CallbackHandler；
        退出上下文时自动结束根 observation 并 ``flush``。
    """

    if not tracing_enabled():
        yield TurnTraceResult(callbacks=[], trace_id=None)
        return

    # 仅包裹「客户端构造 + 根 observation 打开 + CallbackHandler 构造」三段（进入 yield 之前）。
    # 这一段任何失败都属于「可观测性初始化故障」，降级为不追踪（yield 空结果），绝不中断 turn。
    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler

        client = _build_langfuse_client()
        trace_id = str(uuid4())
        trace_metadata: dict[str, str] = {
            "task_id": metadata.task_id,
            "turn_id": metadata.turn_id,
        }
        root_span_cm = client.start_as_current_observation(
            as_type="span", name=f"turn {metadata.turn_id}"
        )
        attr_cm = propagate_attributes(
            session_id=metadata.task_id,
            user_id=metadata.agent_id,
            tags=["coding-agent"],
            metadata=trace_metadata,
        )
    except Exception:
        log.exception(
            "langfuse_turn_trace_failed",
            extra={
                "msg": "Langfuse turn trace 初始化失败，降级为不追踪",
                "data": {"turn_id": metadata.turn_id, "task_id": metadata.task_id},
            },
        )
        yield TurnTraceResult(callbacks=[], trace_id=None)
        return

    # 显式进入根 observation 上下文：若 __enter__ 阶段（可观测性故障）失败，降级为不追踪，
    # 不中断 turn。yield 期间（turn 真实执行）的异常不属于可观测性故障，不在此捕获，
    # 交由 runner 的 try/except 落定为 task_failed，避免吞掉 turn 真实错误。
    try:
        root_span_cm.__enter__()
        attr_cm.__enter__()
    except Exception:
        log.exception(
            "langfuse_turn_trace_failed",
            extra={
                "msg": "Langfuse 根 observation 进入失败，降级为不追踪",
                "data": {"turn_id": metadata.turn_id, "task_id": metadata.task_id},
            },
        )
        yield TurnTraceResult(callbacks=[], trace_id=None)
        return

    handler = CallbackHandler(
        public_key=Settings.LANGFUSE_PUBLIC_KEY,
        trace_context={"trace_id": trace_id},
    )
    try:
        try:
            yield TurnTraceResult(callbacks=[handler], trace_id=trace_id)
        finally:
            # 同步 flush：等待 OTel 后台批量上报队列排空；可能短暂阻塞运行循环，属可接受的
            # 退出成本（不依赖主流程正确性）。
            client.flush()
    finally:
        # 正常结束根 observation（end span）；异常向上传播（turn 真实错误由 runner 落定）。
        try:
            attr_cm.__exit__(None, None, None)
        finally:
            root_span_cm.__exit__(None, None, None)


def flush_langfuse() -> None:
    """尽力 flush 缓冲的 Langfuse trace。

    进程退出前调用（``api/app.py`` lifespan ``finally``）。未启用或客户端创建/上报失败时
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
