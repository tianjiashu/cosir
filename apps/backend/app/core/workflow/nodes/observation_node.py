"""工具观察结算节点：写 canonical tool 消息、投影终态，并把挂起请求交给 prepare_input。"""

from app.config.logging.logger import log
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.node_helper.common import _runtime_context
from app.core.workflow.node_helper.tool_observation_projection import settle_observations


async def _observe_node(state: ReactExecutionState) -> dict[str, object]:
    """结算当前批次并清空摘要，避免下次模型步重复写入。

    工具结果先持久化再发终态；未创建卡片的调用只闭合模型协议。本批有调用要求挂起（等审批、
    等消息）时只记下 ``suspend_requested``，是否真的挂起由 prepare_input 与取消、步数上限一起判定。
    本节点是批次摘要的唯一清算点：下一模型步不应再看到上一批的观察（否则会被连带重新结算）。
    """
    rc = _runtime_context()
    batch = rc.batch
    observations = batch.observations
    step_id = f"step-{state.step_count}"  # 工具是 model 步的延续，复用同一步 step_id
    # 写 canonical tool 消息后再投影终态，冷重建与实时快照使用同一份结果。
    settle_observations(observations, step_id=step_id)
    suspend_requested = batch.suspends()
    rc.batch = ToolBatch()

    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(observations),
                "suspend_requested": suspend_requested,
            },
        },
    )
    return {"suspend_requested": suspend_requested, "next_node": ReactRoute.PREPARE_INPUT}
