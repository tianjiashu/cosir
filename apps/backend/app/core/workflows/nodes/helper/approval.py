"""工具节点「审批」职责独立模块。

本模块只承载「审批编排」单一职责：按 ``RuntimeConfig.approval_resolver`` 决定是否
``interrupt()`` 暂停 graph 等待人工审批，并返回已批准的待执行调用 dict 列表。
与工具执行编排（``_tools_node``）职责分离——本模块不管工具如何执行、取消如何处理、
占位如何闭合、结果如何摘要。

行为契约（与既有 ``_tools_node`` 完全一致）：
- 无审批器（``approval_resolver is None``，含字段缺失的测试桩）→ 自动放行全部调用，
  **不经过 ``interrupt()``**，直接以原始 ``tool_calls`` 作为已批准列表。这样 graph
  不会暂停，编排层循环可正常走到终态，避免「无审批器时反复 interrupt→resume 同一
  工具调用」的死循环。
- 有审批器 → 用 ``interrupt()`` 暂停 graph 等待审批，审批结果经 ``Command(resume=)``
  恢复；恢复值直接 list 用 list，否则（如误传）回退到原始 ``tool_calls``。

``interrupt`` 以参数注入，由 ``_tools_node`` 传入其命名空间内的 ``interrupt``（来自
``langgraph.types``），以便既有测试对 ``tools_node.interrupt`` 的 monkeypatch 仍能生效。

interrupt 载荷结构跨模块共享：产生端（本模块）以 ``{"tool_calls": [...]}`` 交给
``interrupt()``，消费端（``react/workflow.py``）从 ``interrupts[0].value`` 按同键取出。
键名收敛为 :data:`APPROVAL_INTERRUPT_KEY` 单一事实来源，杜绝两处裸键字面量漂移。
"""

from typing import Any

from app.config.logging.logger import log

# interrupt 载荷中「待审批工具调用」的键名（跨模块共享契约，产生端/消费端同用）。
APPROVAL_INTERRUPT_KEY: str = "tool_calls"


def resolve_approved_calls(
    rc: Any,
    tool_calls: list[dict[str, Any]],
    step_id: str,
    interrupt_fn: Any,
) -> list[dict[str, Any]]:
    """按审批器配置裁决待执行工具调用，返回已批准列表。

    无审批器时自动放行（不暂停 graph），直接返回原始 ``tool_calls``；有审批器时
    经 ``interrupt_fn`` 暂停等待人工审批，返回恢复时传入的批准列表（非 list 恢复值
    回退到原始 ``tool_calls``）。

    参数:
        rc: 当前 ``RuntimeConfig``，读取 ``approval_resolver`` 决定是否审批。
        tool_calls: 待审批的工具调用 dict 列表（来自 ``state.pending_tool_calls``）。
        step_id: 当前步标识，用于日志关联与排查。
        interrupt_fn: 审批暂停回调（``langgraph.types.interrupt`` 或测试替身），
            仅在存在审批器时被调用一次。

    返回:
        已批准的调用 dict 列表：无审批器时为 ``tool_calls`` 原样返回；有审批器时为
        恢复时传入的批准列表（非 list 则回退到 ``tool_calls``）。

    异常:
        无（``interrupt_fn`` 自身的暂停/恢复异常由其调用方 LangGraph 处理）。

    副作用:
        有审批器时调用一次 ``interrupt_fn``（暂停 graph 等待审批）；按分支写入
        ``tools_node_auto_approved`` / ``tools_node_started`` 日志。
    """
    # 无审批器（含字段缺失的测试桩）→ 自动放行，不暂停 graph，
    # 直接用原始 tool_calls 作为已批准列表。
    if getattr(rc, "approval_resolver", None) is None:
        log.info(
            "tools_node_auto_approved",
            extra={
                "msg": (
                    f"无审批器，自动放行 {len(tool_calls)} 个工具调用"
                    f"（不暂停 graph），step_id={step_id}"
                ),
                "data": {"step_id": step_id, "pending_tool_count": len(tool_calls)},
            },
        )
        return tool_calls

    log.info(
        "tools_node_started",
        extra={
            "msg": f"工具节点开始执行，等待审批，step_id={step_id}",
            "data": {"step_id": step_id, "pending_tool_count": len(tool_calls)},
        },
    )
    # 核心：interrupt 暂停 graph，把待审批工具调用交出去；外部审批后用
    # Command(resume=approved_list) 恢复，approved 即为恢复时传入的审批结果。
    approved = interrupt_fn({APPROVAL_INTERRUPT_KEY: tool_calls})
    # 兼容两种恢复值：直接 list 用 list，否则（如误传）回退到原始 tool_calls。
    return tool_calls if not isinstance(approved, list) else approved


__all__ = ["APPROVAL_INTERRUPT_KEY", "resolve_approved_calls"]
