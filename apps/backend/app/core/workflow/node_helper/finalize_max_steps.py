"""模型请求超过本次驱动的步骤上界时落定 Run 失败。"""

from typing import Any

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import _runtime_context, route_state
from app.models.enums.conversation_run_status import RunTransition
from app.service import depends as service_depends

# 步数耗尽时写给父 Agent / 用户的默认可见文本（英文，与面向模型的文本约定一致）。
# 正常完成路径的最终回答是模型回答；步数耗尽没有最终回答，故用这段失败说明作为 Run 终态
# 输出，让父 Agent / 用户能感知「因步数耗尽而停止」，而不是静默失败。


async def _finalize_max_steps(step_count: int) -> dict[str, Any]:
    """不再发模型请求；Run 事实写入失败终态后仅返回结束路由。"""

    rc = _runtime_context()
    step_id = f"step-{step_count}"
    run_id = rc.run.id
    failed_run = service_depends.get_conversation_run_state_service().transition(
        run_id,
        RunTransition.FAIL,
        end_reason=Constant.Run.RUN_FAILURE_CODE_MAX_STEPS,
        usage_stats=rc.usage_stats,
        final_output=Constant.Workflow.MAX_STEPS_FINAL_TEXT,
    )
    if failed_run is None:
        log.info(
            "max_steps_node_terminal_race_lost",
            extra={
                "msg": f"最大步数失败落定时 run 已非 running，跳过终态写入，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": run_id},
            },
        )
        return route_state(step_count, ReactRoute.END)

    event_data = {"final_output": Constant.Workflow.MAX_STEPS_FINAL_TEXT}
    log.warning(
        "max_steps_node_failed",
        extra={
            "msg": f"已达到最大步数上限，停止继续执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": step_count,
                "step_limit": rc.step_limit,
                **event_data,
            },
        },
    )
    # 失败原因与终态由 Run 状态服务原子落定；附加诊断
    # 数据只进入结构化日志，不进入事件 payload。
    return route_state(step_count, ReactRoute.END)
