"""工具观察结算节点：写 canonical tool 消息并投影终态；挂起与清空批次归 prepare_input。"""

from app.config.logging.logger import log
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.models.enums.react_route import ReactRoute
from app.core.workflow.node_helper.tool_observation_projection import (
    settle_decision_observation,
    settle_observations,
)


async def _observe_node(state: ReactExecutionState) -> dict[str, object]:
    """结算本批观察后流转到 prepare_input。

    工具结果先持久化再发终态；未创建卡片的调用只闭合模型协议。批次原样留在执行状态里：
    prepare_input 据它判定是否挂起，并在请求模型前清空，下一模型步不会再结算上一批观察。
    """
    batch = state.batch
    step_id = f"step-{state.step_count}"  # 工具是 model 步的延续，复用同一步 step_id
    # 写 canonical tool 消息后再投影终态，冷重建与实时快照使用同一份结果。用户批准后的 HITL
    # 执行不写 tool 消息：占位已是协议结果，结果追加为消息（见 settle_decision_observation）。
    if batch.decision is not None:
        settle_decision_observation(batch, step_id=step_id)
    else:
        settle_observations(batch, step_id=step_id)
    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(batch.observations),
                "suspend_requested": batch.suspends(),
            },
        },
    )
    return {"next_node": ReactRoute.PREPARE_INPUT}
