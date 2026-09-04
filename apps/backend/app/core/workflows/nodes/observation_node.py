"""ReAct-like 工作流的观察节点（``_observe_node``）。

本模块承载「观察节点」的单一职责，作为「工具结果观察处理」的单一收口：在工具执行后，
执行「执行后取消判断」，
再从 ``state.last_tool_results`` 重算连续工具失败计数，并根据 ``Settings.TOOL_ERROR_LIMIT``
判断是否达到错误上限。达到上限时经 ``RuntimeOperations`` 标记终态；否则把更新后的计数
写回 state，让 graph 回到 ``model`` 节点继续推理。

设计动机（与阶段二演进对齐）：
- 「观察工具结果」是一个独立于「执行工具」的推理步骤。阶段一先做确定性的取消判断、
  错误计数/上限判定；阶段二将在此基础上接入 LLM，对
  ``last_tool_results`` 中的 ``content`` / ``error`` / ``reason`` 做判断（例如识别「工具
  结果是否回答了模型的问题」），产出路由信号。把这一步独立成 graph node，使其可独立计步、
  独立审批/重试/取消、独立产事件——本节点即为阶段二「LLM 观察工具结果」的扩展点。
- 落库写回与配对闭合**留在** ``tools`` 节点（见 ``tools_node._persist_tool_observations``），
  本节点只读已落库的结果做判定。即使观察节点崩溃，上下文配对依然完整，避免跨节点撕裂状态。
"""

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.workflows.nodes.helper.common import (
    _runtime_config, _runtime_context,
)

from ..react.state import ReactGraphState
from ...tools.schemas import ToolObservation


async def _observe_node(state: ReactGraphState) -> dict:
    """工具结果观察节点：执行后取消判断 + 重算连续失败计数并判定错误上限。

    本节点是「工具结果观察处理」的单一收口，按固定时序处理：

    1. **执行后取消判断**：工具批次执行后若 run 已取消，落定 cancelled 并置终态
       （``terminal=True``），不进错误计数——工具已执行、结果已写回上下文，取消时不再
       多做一次推理；
    2. **错误计数与上限判定**：从 ``state.last_tool_results``（``tools`` 节点产出的可序列化
       摘要）重算 ``tool_error_count``：任意一次 ``status == "success"`` 即清零（连续失败才
       累计）；仅 ``status == "error"`` 累加计数。``status == "cancelled"`` 属主动中断
       （非工具失败，语义区别于 ``error``），不计入连续失败计数。达到 ``Settings.TOOL_ERROR_LIMIT``
        时经 canonical writer 标记失败终态；否则把更新后的计数写回 state，让 graph 经条件边回到
       ``model`` 节点。

    参数:
        state: 当前 graph state，含本批 ``last_tool_results``、继承的 ``tool_error_count``。

    返回:
        需要合并回 graph state 的增量：执行后取消分支返回 ``{"terminal": True}``；
        空结果批次且未取消返回 ``{}``（不计数、不判定，继承的 ``tool_error_count``
        原样保留——见下方空结果注释）；错误上限分支返回 ``{"tool_error_count",
        "terminal": True}``；否则返回 ``{"tool_error_count"}``，
        供 ``_after_observe`` 路由回 ``model``。

    副作用:
        - 执行后取消分支经 ``RuntimeOperations`` 落定取消终态；
        - 错误上限分支经 ``operations.fail_run_if_running`` 标记 run 失败终态
          （``get_current_run`` 仅在该分支内调用，避免无谓的 DB 读）；
        - 阶段二将在此接入 LLM 观察推理并写入明确的观察事实，不在此写消息通道。
    """
    rc = _runtime_config()  # 取运行时配置（含 operations / langfuse_trace_id）
    operations = rc.operations  # 领域操作
    observations: list[ToolObservation] = state.last_tool_results["observations"]  # tools 节点产出的结果摘要
    instruction = state.last_tool_results["instruction"]
    run_id = operations.get_current_run().id
    # 1. 执行后取消判断：工具已执行完毕（结果已写回上下文闭合配对），若 run 取消则不再
    # 多做一次推理并置取消终态，不进错误计数。此判断优先于空结果/错误计数，
    # 保证取消场景无论结果有无都走统一终态。
    if operations.is_current_run_cancelled():
        log.info(
            "observe_node_cancelled_after_execution",
            extra={
                "msg": (
                    "工具结果观察时检测到 run 已取消，停止后续模型调用，"
                    f"step_id=step-{state.step_count}"
                ),
                "data": {"step_id": f"step-{state.step_count}"},
            },
        )
        # 取消终态经 RuntimeOperations 条件落定，直接结束。
        operations.cancel_run_if_running(end_reason="run_cancelled_after_execution", usage_stats=rc.usage_stats)
        return {"terminal": True}

    if not observations:
        # 无本批工具结果：不计数也不判定，避免对无新结果时误发 RUN_FAILED。
        # 「连续失败计数滞留」是有意为之——本批没有任何 success/error 信号，既无法证明
        # 连续失败在延续，也无法证明已中断；无信息即不改写，把继承的 tool_error_count
        # 原样保留，等下一批有实际结果时再按信号重置/累加。
        log.info(
            "observe_node_no_results",
            extra={
                "msg": (
                    f"无本批工具结果，跳过观察判定，保留继承计数，"
                    f"step_id=step-{state.step_count}"
                ),
                "data": {
                    "step_id": f"step-{state.step_count}",
                    "tool_error_count": state.tool_error_count,
                },
            },
        )
        return {}


    tool_error_count = state.tool_error_count  # 从 state 继承连续失败计数
    error_count = 0  # 本批错误数（与连续失败数在同一循环累加，避免二次遍历）
    for observation in observations:
        if observation.status == "success":
            tool_error_count = 0  # 成功则清零（连续失败才累计）
        elif observation.status == "error":
            tool_error_count += 1  # 工具失败 +1
            error_count += 1
        # "cancelled"（主动中断）不计入连续失败计数：非工具失败，语义区别于 error。
        model_message = operations._to_model_message(observation)
        _runtime_context().add_message(model_message)


    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id=step-{state.step_count}",
            "data": {
                "step_id": f"step-{state.step_count}",
                "result_count": len(observations),
                "error_count": error_count,
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Settings.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        failed_run = operations.fail_run_if_running(end_reason="tool_error_limit_reached", usage_stats=rc.usage_stats)
        if failed_run is None:
            log.info(
                "observe_node_error_limit_terminal_race_lost",
                extra={
                    "msg": (
                        f"工具错误上限失败落定时 run 已非 running，"
                        f"跳过失败事件，step_id=step-{state.step_count}"
                    ),
                    "data": {"step_id": f"step-{state.step_count}", "run_id": run_id},
                },
            )
            return {
                "tool_error_count": tool_error_count,
                "terminal": True,
            }
        log.warning(
            "observe_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id=step-{state.step_count}",
                "data": {
                    "step_id": f"step-{state.step_count}",
                    "tool_error_count": tool_error_count,
                    "limit": Settings.TOOL_ERROR_LIMIT,
                    "instruction": instruction,
                },
            },
        )
        # 失败终态：错误上限时经 canonical writer 落定失败。
        return {
            "tool_error_count": tool_error_count,
            "terminal": True,
        }

    # 正常返回：把更新后的计数写回 state。
    return {"tool_error_count": tool_error_count}
