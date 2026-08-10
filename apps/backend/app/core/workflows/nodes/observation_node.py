"""ReAct-like 工作流的观察节点（``_observe_node``）。

本模块只承载「观察节点」单一职责：在工具执行后，从 ``state.last_tool_results`` 重算
连续工具失败计数，并根据 ``Settings.TOOL_ERROR_LIMIT`` 判断是否达到错误上限。达到上限
时标记终态并发 ``RUN_FAILED`` 事件；否则把更新后的计数写回 state，让 graph 回到 ``model``
节点继续推理。

设计动机（与阶段二演进对齐）：
- 「观察工具结果」是一个独立于「执行工具」的推理步骤。阶段一只做确定性的错误计数与
  上限判定；阶段二将在此基础上接入 LLM，对 ``last_tool_results`` 中的 ``content`` /
  ``error`` / ``reason`` 做判断（例如识别「工具结果是否回答了模型的问题」），产出路由
  信号。把这一步独立成 graph node，使其可独立计步、独立审批/重试/取消、独立产事件。
- 落库写回**留在** ``tools`` 节点（见 ``tools_node._persist_tool_observations``），本节点
  只读已落库的结果做判定。即使观察节点崩溃，上下文配对依然完整，避免跨节点撕裂状态。
"""

from app.config.logging.logger import log
from app.config.settings import Settings
from app.models.enums.event_type import EventType
from app.models.payload import RunFailedPayload

from ..react.state import ReactGraphState
from .common import _runtime_config, write_event


async def _observe_node(state: ReactGraphState) -> dict:
    """工具结果观察节点：重算连续失败计数并判定错误上限。

    从 ``state.last_tool_results``（``tools`` 节点产出的可序列化摘要）重算
    ``tool_error_count``：任意一次 ``status == "success"`` 即清零（连续失败才累计），
    否则 +1。达到 ``Settings.TOOL_ERROR_LIMIT`` 时标记终态并发 ``RUN_FAILED``；
    否则把更新后的计数写回 state，让 graph 经条件边回到 ``model`` 节点。

    参数:
        state: 当前 graph state，含本批 ``last_tool_results`` 与继承的 ``tool_error_count``。

    返回:
        需要合并回 graph state 的增量：空结果批次直接返回 ``{}``（不计数、不判定）；
        错误上限分支返回 ``{"tool_error_count", "terminal": True}``；否则返回更新后的
        ``{"tool_error_count"}``，供 ``_after_observe`` 路由回 ``model``。

    副作用:
        - 错误上限分支经 ``write_event`` 发 ``RUN_FAILED``，并经 ``operations.fail_turn_if_running``
          标记 turn 失败终态（``get_current_turn`` 仅在该分支内调用，避免无谓的 DB 读）；
        - 阶段二将在此接入 LLM 观察推理并产 ``OBSERVATION_ADDED`` 类事件，不在此写消息通道。
    """
    rc = _runtime_config()  # 取运行时配置（含 operations / langfuse_trace_id）
    operations = rc.operations  # 领域操作
    results = state.last_tool_results  # tools 节点产出的结果摘要

    if not results:  # 无结果批次不计数也不判定，避免对无新结果时误发 RUN_FAILED。
        log.info(
            "observe_node_no_results",
            extra={
                "msg": f"无本批工具结果，跳过观察判定，step_id=step-{state.step_count}",
                "data": {
                    "step_id": f"step-{state.step_count}",
                    "tool_error_count": state.tool_error_count,
                },
            },
        )
        return {}

    tool_error_count = state.tool_error_count  # 从 state 继承连续失败计数
    error_count = 0  # 本批错误数（与连续失败数在同一循环累加，避免二次遍历）
    for result in results:
        if result.get("status") == "success":
            tool_error_count = 0  # 成功则清零（连续失败才累计）
        else:
            tool_error_count += 1  # 失败 +1
            error_count += 1

    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id=step-{state.step_count}",
            "data": {
                "step_id": f"step-{state.step_count}",
                "result_count": len(results),
                "error_count": error_count,
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Settings.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        turn = operations.get_current_turn()  # 当前 turn 记录（仅错误上限分支需要 turn_id）
        failed_turn = operations.fail_turn_if_running(
            turn.turn_id, end_reason="tool_error_limit_reached"
        )
        if failed_turn is None:
            log.info(
                "observe_node_error_limit_terminal_race_lost",
                extra={
                    "msg": (
                        f"工具错误上限失败落定时 turn 已非 running，"
                        f"跳过失败事件，step_id=step-{state.step_count}"
                    ),
                    "data": {"step_id": f"step-{state.step_count}", "turn_id": turn.turn_id},
                },
            )
            return {"tool_error_count": tool_error_count, "terminal": True}
        log.warning(
            "observe_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id=step-{state.step_count}",
                "data": {
                    "step_id": f"step-{state.step_count}",
                    "tool_error_count": tool_error_count,
                    "limit": Settings.TOOL_ERROR_LIMIT,
                    "instructions": [
                        r.get("instruction", "")
                        for r in results
                        if r.get("instruction")
                    ],
                },
            },
        )
        write_event(
            EventType.RUN_FAILED,
            RunFailedPayload(
                step_id=f"step-{state.step_count}",
                status="failed",
                error="tool_error_limit_reached",
                tool_name=results[0].get("tool_name", ""),
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        return {"tool_error_count": tool_error_count, "terminal": True}  # 失败终态

    return {"tool_error_count": tool_error_count}
