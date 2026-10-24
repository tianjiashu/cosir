"""执行当前模型批次的工具调用；结果写入与挂起判定归 observe 节点。

续跑与唤醒都从模型边界开始，不自动重放工具。工具执行的外部副作用不能随 context 编辑或取消回滚。"""

import dataclasses
from typing import Any

from app.config.logging.logger import log
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import _runtime_context
from app.core.workflow.node_helper.tool_batch_runner import run_tool_batch


def _summary(observation: ToolObservation) -> dict[str, Any]:
    """观察摘要只保留模型结算与挂起判定所需字段，内部产物不进入 Run。"""
    result = dataclasses.asdict(observation)
    result.pop("artifact_data", None)
    return result


async def _tools_node(state: ReactExecutionState) -> dict[str, Any]:
    """执行当前模型批次后流转到 observe 节点。

    参数串原样交给执行器，解码与校验归 ``ToolAccessGate``，畸形参数在那里形成错误观察。结果只并入
    本批摘要，canonical tool 消息、终态事件与是否挂起统一由 observe 节点结算。
    """
    rc = _runtime_context()
    batch = rc.batch
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    tool_calls = [
        ToolCall(
            tool_name=call["function"]["name"],
            arguments=call["function"]["arguments"],
            call_id=call["id"],
        )
        for call in batch.calls
    ]
    log.info(
        "tools_node_started",
        extra={
            "msg": f"准备执行 {len(tool_calls)} 个工具调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "call_count": len(tool_calls),
                "call_ids": [call.call_id for call in tool_calls],
            },
        },
    )
    # running 与终态由 run_tool_batch 按调用投影：只有真正开始运行的调用才显示为运行中。
    observations = await run_tool_batch(rc, tool_calls, step_id)
    summaries = [_summary(observation) for observation in observations]
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具批次处理完成，step_id={step_id}",
            "data": {"step_id": step_id, "observations": summaries},
        },
    )
    batch.observations = summaries
    return {"next_node": ReactRoute.OBSERVE}
