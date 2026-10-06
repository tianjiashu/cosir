"""构造最大推理步数耗尽后的最终回答路由。"""

from typing import Any

from app.config.logging.logger import log
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState


async def _finalize_max_steps(
    state: ReactGraphState, *, step_count: int | None = None
) -> dict[str, Any]:
    """在普通推理步数耗尽后保留一次无工具最终回答请求。

    参数:
        state: 当前 graph state。
        step_count: 超出普通推理预算的步骤号，仅用于日志；省略时使用 state 中的下一步号。

    返回:
        把工作流路由回 model 并开启 ``final_answer_only`` 的 state patch。

    异常:
        无。

    副作用:
        写入最大步数收口日志；不迁移 Run 状态，Run 由最终回答请求决定完成或失败。
    """

    effective_step_count = state.step_count + 1 if step_count is None else step_count
    log.warning(
        "max_steps_final_answer_started",
        extra={
            "msg": "普通推理步数已耗尽，切换到无工具最终回答",
            "data": {
                "step_count": effective_step_count,
                "max_steps": state.max_steps,
                "final_answer_only": True,
            },
        },
    )
    return {
        "next_node": ReactRoute.MODEL,
        "final_answer_only": True,
        "tool_request": {},
        "tool_feedback": "",
    }
