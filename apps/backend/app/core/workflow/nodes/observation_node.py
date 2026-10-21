"""工具观察结算节点：写 canonical tool 消息、投影终态并决定是否继续模型。"""

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.node_helper.common import _runtime_context
from app.core.workflow.node_helper.tool_observation_projection import settle_observations
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends


async def _observe_node(state: ReactExecutionState) -> dict[str, object]:
    """结算当前批次并清空摘要，避免下次模型步重复写入或重复计错。

    工具结果先持久化再发终态；未创建卡片的调用只闭合模型协议。连续不可重试失败
    达上限时由 Run 状态服务落定失败，其余情况返回模型节点。
    """
    rc = _runtime_context()
    batch = rc.batch
    observations = batch.observations
    run_id = rc.run.id
    step_id = f"step-{state.step_count}"  # 工具是 model 步的延续，复用同一步 step_id
    # 写 canonical tool 消息后再投影终态，冷重建与实时快照使用同一份结果。
    tool_error_count, error_count = settle_observations(
        observations,
        step_id=step_id,
        inherited_error_count=state.tool_error_count,
    )
    rc.batch = ToolBatch()

    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(observations),
                "error_count": error_count,
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Constant.Workflow.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        failed_run = service_depends.get_conversation_run_state_service().transition(
            run_id,
            RunTransition.FAIL,
            end_reason=Constant.Run.RUN_FAILURE_CODE_TOOL_ERROR_LIMIT,
            usage_stats=rc.usage_stats,
        )
        if failed_run is None:
            log.info(
                "observe_node_error_limit_terminal_race_lost",
                extra={
                    "msg": (
                        f"工具错误上限失败落定时 run 已非 running，"
                        f"跳过失败事件，step_id={step_id}"
                    ),
                    "data": {"step_id": step_id, "run_id": run_id},
                },
            )
            return {
                "tool_error_count": tool_error_count,
                "next_node": ReactRoute.END,
            }
        log.warning(
            "observe_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_error_count": tool_error_count,
                    "limit": Constant.Workflow.TOOL_ERROR_LIMIT,
                },
            },
        )
        # 失败终态：错误上限时经 canonical writer 落定失败。
        return {
            "tool_error_count": tool_error_count,
            "next_node": ReactRoute.END,
        }

    # 正常返回：把更新后的计数写回 state，并清空已结算的本批摘要——本节点是
    # 「工具结果观察处理」的单一收口，也是批次摘要的唯一清算点：下一模型步不应再看到上一批的观察
    # （否则会被连带重新结算，见上文批次边界说明）。
    return {
        "tool_error_count": tool_error_count,
        "next_node": ReactRoute.MODEL,
    }
