"""工具观察分发器：把一批观察摘要分发到「前端事件流 + 模型上下文」。

``observe`` 节点消费 ``tools`` 节点产出的本批观察摘要（见
``tool_observation_summary``，已丢 ``data``、已脱敏截断，是唯一允许进入 graph
state / checkpoint 的形状）后，由本模块做统一分发：

1. **事件流**：逐条发 :class:`ToolCallStatusChangedEvent` 终态事件
   （``success→completed`` / ``error→failed`` / ``cancelled→cancelled``），
   ``completed`` 携带 ``result=content``、``failed`` 携带 ``error``；
2. **模型上下文**：逐条经 ``WorkflowOperations._to_model_message`` 转
   ``ToolMessage`` 写回 ``RuntimeContextManager``，闭合上一轮 ``AIMessage.tool_calls``
   配对（模型上下文由 RuntimeContextManager 独占，不进 graph state）；
3. **计数**：``success`` 清零、``error`` 累加、``cancelled`` 不计入（主动中断
   非工具失败），返回连续失败计数与本批错误数。

终态事件统一在 observe 阶段发出是工具结果处理的单一收口：执行层只产出事实，
前端可见性与模型可见性在观察节点一次性落定。

职责边界：
- 负责：观察摘要 → 终态事件 + 模型上下文消息的分发与计数。
- 不负责：摘要的生成（``tool_observation_summary``）、取消终态落定与错误上限
  判定（observation_node 编排）、事件投影到 snapshot（ConversationEventProjector）。
"""

from typing import Any, Literal

from langgraph.config import get_stream_writer

from app.config.logging.logger import log
from app.core.tools.schemas import ToolObservation
from app.core.workflows.event import ToolCallStatusChangedEvent
from app.core.workflows.nodes.helper.common import _runtime_config, _runtime_context


def _event_status(status: str) -> Literal["completed", "failed", "cancelled"]:
    """把观察摘要状态映射为事件契约的终态状态。

    参数:
        status: 观察摘要的原始状态（``success`` / ``error`` / ``cancelled``）。

    返回:
        ``"completed"``（success）/ ``"cancelled"``（cancelled）/ ``"failed"``
        （error 及未知状态兜底），保证前端 tool-call part 必达终态。

    异常:
        无（纯函数）。

    副作用:
        无。
    """

    if status == "success":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    return "failed"


def _summary_to_observation(summary: dict[str, Any]) -> ToolObservation:
    """把一条观察摘要转回 ``ToolObservation`` 供模型消息序列化消费。

    ``WorkflowOperations._to_model_message`` 的入参契约是
    :class:`ToolObservation`；摘要本身是可序列化 dict，转回值对象时 ``data``
    置 ``None``（展示通道数据本就不进摘要、不到模型）。

    参数:
        summary: ``tool_observation_summary`` 产出的单条摘要 dict。

    返回:
        等价语义的 :class:`ToolObservation`（``data=None``）。

    异常:
        KeyError: 摘要缺失必需字段时抛出（上游契约被破坏，属装配错误，
            不做静默降级）。

    副作用:
        无（仅构造新对象）。
    """

    return ToolObservation(
        tool_name=summary["tool_name"],
        status=summary["status"],
        content=summary["content"],
        error=summary["error"],
        reason=summary["reason"],
        retryable=summary["retryable"],
        tool_call_id=summary["call_id"],
        data=None,
    )


def dispatch_tool_observations(
    summaries: list[dict[str, Any]],
    *,
    task_id: int,
    run_id: int,
    step_id: str,
    inherited_error_count: int,
) -> dict[str, Any]:
    """把一批观察摘要分发到事件流与模型上下文，并返回重算后的连续失败计数。

    对每条摘要依次执行「发终态事件 → 写模型上下文 → 计数」。事件先于上下文写入，
    使前端先看到工具结果、模型推理稍后消费；逐条处理保证顺序与批次一致。

    参数:
        summaries: 本批次观察摘要（``tools`` 节点经
            ``tool_observation_summary`` 产出，已完成执行层预算治理）。
        task_id: 当前任务 id，事件路由用。
        run_id: 当前 run id，事件路由用。
        step_id: 当前 step 标识（工具是 model 步的延续，复用 model 步的 step_id）。
        inherited_error_count: 从 graph state 继承的连续失败计数。

    返回:
        ``{"tool_error_count": int, "error_count": int}``：重算后的连续失败计数
        与本批错误数（供 observe 节点日志与上限判定）。

    异常:
        KeyError: 摘要缺失必需字段时向上传播（上游契约被破坏）。

    副作用:
        - 经 LangGraph stream writer 发出 ``ToolCallStatusChangedEvent``；
        - 经 ``WorkflowOperations._to_model_message`` + ``RuntimeContextManager``
          把观察写回模型上下文（闭合工具调用配对）。
    """

    stream_writer = get_stream_writer()
    operations = _runtime_config().operations
    tool_error_count = inherited_error_count
    error_count = 0

    for summary in summaries:
        status = summary["status"]
        event_status = _event_status(status)
        # 1. 终态事件：前端 tool-call part 由 running 迁移到目标终态。
        stream_writer(
            ToolCallStatusChangedEvent(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=summary["call_id"],
                status=event_status,
                result=summary["content"] if event_status == "completed" else None,
                error=summary["error"] if event_status in {"failed", "cancelled"} else None,
            )
        )
        # 2. 模型上下文：转 ToolMessage 写回 RuntimeContextManager，闭合
        #    上一轮 AIMessage.tool_calls 配对。模型上下文由 RuntimeContextManager
        #    独占，不进 graph state。
        _runtime_context().add_message(
            operations.to_tool_model_message(_summary_to_observation(summary))
        )
        # 3. 计数：completed 清零、failed 累加；cancelled 是主动中断而非工具失败，
        #    不计入连续失败。按事件终态计数（而非原始状态），保证未知状态兜底为
        #    failed 时同样计入，与前端可见性口径一致。
        if event_status == "completed":
            tool_error_count = 0
        elif event_status == "failed":
            tool_error_count += 1
            error_count += 1

    log.info(
        "observe_node_dispatch_completed",
        extra={
            "msg": f"工具观察分发完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(summaries),
                "error_count": error_count,
                "tool_error_count": tool_error_count,
            },
        },
    )
    return {"tool_error_count": tool_error_count, "error_count": error_count}
