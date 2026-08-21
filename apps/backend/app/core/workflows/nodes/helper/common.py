"""ReAct-like 工作流节点间共享的运行时辅助。

本模块只承载「model / tools / observe 三个节点都要用」的公共原语，不包含任何单节点专属逻辑：

- ``write_event`` / ``_make_write_event``：统一的事件写入结构。
- ``_runtime_config`` / ``_runtime_context``：从 LangGraph 运行上下文取运行时配置与
  task 级上下文。
- ``build_run_failed_payload``：统一构造携带 token 摘要的 ``RunFailedPayload``，消除
  各节点重复展开 ``usage_stats.to_dict()`` 六字段的样板。
- ``emit_run_cancelled``：统一构造并写出 ``RUN_CANCELLED`` 取消终态事件，消除
  model / tools / observe 三节点各自手写 ``RunCancelledPayload`` 导致字段口径
  不一致（如遗漏 ``langfuse_trace_id``）的重复与可排查性隐患。
- ``terminal_state``：统一构造终态 state patch，消除各节点
  重复的 ``{"terminal": True, ...}`` 字典字面量。

节点各自的数据处理辅助不放这里；``content → text`` 归一统一收口于
``app.utils.message_content.content_to_text``（原 ``chunk_assembler._extract_text``
与 ``runtime_context_manager._content_to_text`` 两套同构口径已合并至此）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from langgraph.config import get_config, get_stream_writer

from app.models.enums.event_type import EventType
from app.models.payload import RunCancelledPayload, RunFailedPayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload
from app.models.turn_usage_stats import TurnUsageStats

if TYPE_CHECKING:
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.core.workflows.react.runtime_config import RuntimeConfig


# 内部封装：统一事件写入结构。
def write_event(event_type: EventType, payload: RuntimeEventPayload) -> None:
    """在当前 LangGraph 运行上下文写出一条自定义业务事件。

    参数:
        event_type: 事件类型枚举。
        payload: 事件负载值对象。

    返回:
        无。

    异常:
        RuntimeError: 在无 LangGraph 运行上下文处调用时由 ``get_stream_writer()`` 抛出。

    副作用:
        经 ``get_stream_writer()`` 写入 ``custom`` 事件流。
    """
    writer = get_stream_writer()  # 自定义事件写入器
    writer({"event_type": str(event_type), "payload": payload})


def _make_write_event() -> Callable[[EventType, RuntimeEventPayload], None]:
    """在当前 LangGraph 运行上下文中取出 writer，返回可跨线程调用的事件写入回调。

    ``write_event`` 每次调用都现取 ``get_stream_writer()``，只能在持有 LangGraph
    运行上下文的协程内使用。当节点把工作交给 ``asyncio.to_thread`` 时，需要先在
    协程内取出 writer 对象并由闭包持有，工作线程才能继续写 ``custom`` 事件流。

    返回:
        与 ``write_event`` 同签名的回调，内部使用已捕获的 writer。

    异常:
        RuntimeError: 在无 LangGraph 运行上下文处调用时由 ``get_stream_writer()`` 抛出。

    副作用:
        无（调用返回的回调时才写入事件流）。
    """
    writer = get_stream_writer()

    def _write(event_type: EventType, payload: RuntimeEventPayload) -> None:
        writer({"event_type": str(event_type), "payload": payload})

    return _write


def _runtime_config() -> RuntimeConfig:
    """从 LangGraph 运行上下文取出 ReAct 工作流注入的运行时配置容器。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeConfig`` 放入 config 的 ``runtime_config``；
    节点统一经本函数取出，避免在各节点里用裸字符串 key 重复读取 ``config["configurable"]``。

    返回:
        当前 graph 执行注入的 ``RuntimeConfig`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeConfig。
    return get_config()["configurable"]["runtime_config"]


def _runtime_context() -> RuntimeContextManager:
    """从 LangGraph 运行上下文取出 task 级运行时上下文。

    ``ReactLikeWorkflow.run()`` 把 ``RuntimeContextManager`` 放入 config 的 ``runtime_context``；
    节点统一经本函数取出，与 ``_runtime_config`` 同口径，避免裸字符串 key 重复读取
    ``config["configurable"]``，且使上下文对象不进入 graph state（不兼容消息 reducer）。

    返回:
        当前 graph 执行注入的 ``RuntimeContextManager`` 实例。
    """
    # 从 LangGraph 注入的 config 中取出预先放好的 RuntimeContextManager。
    return get_config()["configurable"]["runtime_context"]


def build_run_failed_payload(
    step_id: str,
    error: str,
    *,
    usage: TurnUsageStats,
    langfuse_trace_id: str | None,
    status: Literal["failed"] | None = "failed",
    data: dict[str, Any] | None = None,
) -> RunFailedPayload:
    """统一构造携带 token 摘要的 ``RunFailedPayload``。

    各失败分支（模型取消 / 非法工具 / 无效输出 / 步数耗尽 / 工具连续失败）都需要展开
    ``usage_stats.to_dict()`` 的六个扁平 token 字段，手写易漏字段且重复。本函数把该
    样板收口为单一构造点，杜绝字段不一致。

    参数:
        step_id: 失败发生的步标识（如 ``step-3``）；同时写入 payload 的 ``step_id``。
        error: 面向排查者的英文/中文错误描述（镜像进 ``content``）；不得含 secret。
        usage: 截至失败时的累计 token 统计；其 ``to_dict()`` 的六个字段原样展开进 payload。
        langfuse_trace_id: 关联的 Langfuse trace id，无则传 ``None``（不写占位串）。
        status: 事件状态字段，默认 ``"failed"``；非默认分支（如非法工具分类）可显式传入。
        data: 可选附加数据（如非法工具调用的分类明细），无则不写 ``data`` 字段。

    返回:
        字段完整、可直接发 ``RUN_FAILED`` 事件的 ``RunFailedPayload`` 实例。

    异常:
        无（纯数据装配；``usage.to_dict()`` 不会抛）。

    副作用:
        无。
    """
    usage_dict = usage.to_dict()
    return RunFailedPayload(
        step_id=step_id,
        error=error,
        status=status,
        langfuse_trace_id=langfuse_trace_id,
        data=data,
        input_tokens=usage_dict["input_tokens"],
        output_tokens=usage_dict["output_tokens"],
        total_tokens=usage_dict["total_tokens"],
        cache_hit_tokens=usage_dict["cache_hit_tokens"],
        cache_miss_tokens=usage_dict["cache_miss_tokens"],
        reasoning_tokens=usage_dict["reasoning_tokens"],
    )


def emit_run_cancelled(rc: RuntimeConfig, step_id: str) -> None:
    """发出 turn 取消终态事件（``RUN_CANCELLED``），携带本轮已消耗 token 摘要与 trace id。

    model / tools / observe 三个节点在检测到 turn 取消时都要发同构的
    ``RUN_CANCELLED`` 事件。手写会导致两处问题：一是 ``RunCancelledPayload`` 字段
    在多处分歧（例如漏写 ``langfuse_trace_id`` 造成 Langfuse 追踪断链、可排查性下降），
    二是未来字段变更需同步多处易漏改。本函数把该构造收口为单一来源，三节点统一调用。

    参数:
        rc: 当前 ``RuntimeConfig``（取 ``usage_stats`` / ``langfuse_trace_id``）。
        step_id: 触发取消的步唯一标识，写入事件供前端关联。

    返回:
        无。

    异常:
        无。

    副作用:
        经 ``write_event`` 写入一条 ``EventType.RUN_CANCELLED`` 事件。
    """
    usage_summary = rc.usage_stats.to_dict()
    write_event(
        EventType.RUN_CANCELLED,
        RunCancelledPayload(
            status="cancelled",
            step_id=step_id,
            error="turn_cancelled",
            langfuse_trace_id=rc.langfuse_trace_id,
            input_tokens=usage_summary["input_tokens"],
            output_tokens=usage_summary["output_tokens"],
            total_tokens=usage_summary["total_tokens"],
            cache_hit_tokens=usage_summary["cache_hit_tokens"],
            cache_miss_tokens=usage_summary["cache_miss_tokens"],
            reasoning_tokens=usage_summary["reasoning_tokens"],
        ),
    )


def terminal_state(
    step_count: int,
    *,
    repair_requested: bool = False,
    requested_tool: bool = False,
    final_response: bool = False,
) -> dict[str, Any]:
    """构造统一的终态 state patch（graph 走到 END 用）。

    model / max_steps / observe 多个节点都把「终态」写成一组重复的硬字段字典
    （``step_count`` / ``repair_requested`` / ``requested_tool`` / ``final_response`` /
    ``terminal`` / ``pending_tool_calls``），手写易错且各处分歧。本函数收口为单一来源。
    终态不再有后续模型步，故统一清空 ``deferred_repair_message``，避免残留进 checkpoint
    与 observe 节点各分支的手动清空口径保持一致（P2-5 一致性收口）。

    参数:
        step_count: 当前步编号，直接落入 patch。
        repair_requested: 是否需要修复重写，``bool`` 类型，与 ``ReactGraphState.repair_requested``
            声明一致（历史遗留的 ``str`` 三值语义已收敛为纯 ``bool``），默认 ``False``。
        requested_tool: 本步是否请求了工具，默认 ``False``。
        final_response: 是否产出终态文本，默认 ``False``。

    返回:
        可直接 ``return`` 给 LangGraph 合并的 state patch 字典
        （``pending_tool_calls`` 恒为 ``[]``、``deferred_repair_message`` 恒为 ``""``）。

    异常:
        无。

    副作用:
        无。
    """
    return {
        "step_count": step_count,
        "repair_requested": repair_requested,
        "requested_tool": requested_tool,
        "final_response": final_response,
        "terminal": True,
        "pending_tool_calls": [],
        "deferred_repair_message": "",
    }
