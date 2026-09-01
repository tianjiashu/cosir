"""ReAct-like 工作流节点间共享的运行时辅助。

本模块只承载「model / tools / observe 三个节点都要用」的公共原语，不包含任何单节点专属逻辑：

- ``_make_write_event``：为工具执行服务提供 canonical fact writer 适配。
- ``_runtime_config`` / ``_runtime_context``：从 LangGraph 运行上下文取运行时配置与
  task 级上下文。
- ``emit_run_cancelled``：统一经 ``RuntimeOperations`` 条件落定取消终态。
- ``terminal_state``：统一构造终态 state patch，消除各节点
  重复的 ``{"terminal": True, ...}`` 字典字面量。

节点各自的数据处理辅助不放这里；``content → text`` 归一统一收口于
``app.utils.message_content.content_to_text``（原 ``chunk_assembler._extract_text``
与 ``runtime_context_manager._content_to_text`` 两套同构口径已合并至此）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from langgraph.config import get_config

if TYPE_CHECKING:
    from app.core.context.runtime_context_manager import RuntimeContextManager
    from app.core.workflows.react.runtime_config import RuntimeConfig


def _make_write_event() -> Callable[..., None]:
    """返回供工具执行服务使用的 canonical tool-fact writer 回调。

    工具执行服务当前仍以两个位置参数描述工具生命周期写入，但这里不再把旧事件
    类型或 payload 类型带入 workflow 节点层；回调直接落到 ``RuntimeOperations`` 的
    canonical writer 入口。它只用于 ``tools_node`` 的跨线程工具执行适配，不广播事件。

    返回:
        可供工具执行服务调用的 writer 回调。

    异常:
        RuntimeError: 在无 LangGraph 运行上下文处调用时由 ``get_config()`` 抛出。

    副作用:
        回调被调用时，经 ``RuntimeOperations`` 写入工具调用事实。
    """
    return cast(Callable[..., None], _runtime_config().operations.record_runtime_payload)


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


def emit_run_cancelled(rc: RuntimeConfig, step_id: str) -> None:
    """经 ``RuntimeOperations`` 条件落定取消终态。

    参数:
        rc: 当前 ``RuntimeConfig``。
        step_id: 触发取消的步唯一标识，仅用于结构化日志关联。

    返回:
        无。

    异常:
        透传 canonical writer 的持久化异常。

    副作用:
        经 ``RuntimeOperations`` 条件写入 cancelled 运行终态；已落定的终态不会被覆盖。
    """
    rc.operations.cancel_turn_if_running(
        rc.turn.id,
        end_reason="runtime_cancelled",
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
