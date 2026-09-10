"""ReAct-like 工作流的观察节点（``_observe_node``）。

本模块承载「观察节点」的单一职责，作为「工具结果观察处理」的**单一收口**：
``tools`` 节点执行完工具批次后，本节点按固定时序处理本批观察：

1. **执行后取消判断**：run 已取消时先分发本批结果（终态事件 + 模型上下文），
   再落定取消终态——工具已执行、结果已产生，取消不能让前端 tool-call part
   永久停留在 running；
2. **结果分发**：经 ``tool_observation_dispatcher`` 把已治理观察分发到前端事件流
   （``ToolCallStatusChangedEvent`` 终态）与模型上下文（``ToolMessage`` 配对闭合）；
3. **错误计数与上限判定**：从分发结果重算连续失败计数，达到
   ``Settings.TOOL_ERROR_LIMIT`` 时经 ``RuntimeOperations`` 标记失败终态；
   否则写回计数，让 graph 经条件边回到 ``model`` 节点继续推理。

设计动机（与阶段二演进对齐）：
- 「观察工具结果」是独立于「执行工具」的推理步骤。阶段一做确定性的分发、取消
  判断、错误计数/上限判定；阶段二将在此基础上接入 LLM，对摘要中的 ``content`` /
  ``error`` / ``reason`` 做判断（例如识别「工具结果是否回答了模型的问题」），产出
  路由信号。把这一步独立成 graph node，使其可独立计步、独立审批/重试/取消、
  独立产事件——本节点即为阶段二「LLM 观察工具结果」的扩展点。
- 本节点只读 ``tools`` 节点产出的摘要/观察做分发与判定，不再补落库与配对闭合
  之外的状态写入；即使观察节点崩溃，run 终态路径上的 ``ToolCallsSettledEvent``
  仍会兜底收束未决 tool-call part。
"""

from typing import Any

from langchain_core.messages import SystemMessage

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.workflows.nodes.helper.common import _runtime_config, _runtime_context
from app.core.workflows.nodes.helper.invalid_tool_call import TOOL_CALL_REPAIR_MESSAGE_KIND
from app.core.workflows.nodes.helper.tool_observation_dispatcher import (
    dispatch_tool_observations,
)

from ..react.state import ReactGraphState


async def _observe_node(state: ReactGraphState) -> dict:
    """工具结果观察节点：终态事件分发 + 模型上下文写回 + 取消判断 + 错误上限判定。

    本节点是「工具结果观察处理」的单一收口，按固定时序处理：

    1. **结果分发**：对本批 ``last_tool_results`` 的观察逐条发出
       ``ToolCallStatusChangedEvent`` 终态事件（``completed`` / ``failed`` /
       ``cancelled``）并写回模型上下文（``ToolMessage``，闭合
       ``AIMessage.tool_calls`` 配对）。事件先于取消判断，保证取消场景下前端
       tool-call part 也能到达终态；同时重算连续失败计数（``success`` 清零、
       ``error`` 累加、``cancelled`` 不计）。
    2. **执行后取消判断**：分发完成后若 run 已取消，落定 cancelled 终态
       （``terminal=True``）并结束，不再进错误上限判定与后续推理。
    3. **空结果批次**：无本批工具结果时不计数也不判定，保留继承的
       ``tool_error_count``（无信息即不改写），避免对无新结果时误发 RUN_FAILED。
    4. **错误上限判定**：连续失败计数达到 ``Settings.TOOL_ERROR_LIMIT`` 时经
       ``RuntimeOperations.fail_run_if_running`` 标记失败终态；否则写回计数，
       让 graph 经条件边回到 ``model`` 节点。

    参数:
        state: 当前 graph state，含本批 ``last_tool_results``、继承的 ``tool_error_count``。

    返回:
        需要合并回 graph state 的增量：执行后取消分支返回 ``{"tool_error_count",
        "terminal": True}``；空结果批次且未取消返回 ``{}``；错误上限分支返回
        ``{"tool_error_count", "terminal": True}``；否则返回
        ``{"tool_error_count"}``，供 ``_after_observe`` 路由回 ``model``。

    副作用:
        - 分发阶段经 stream writer 发出 ``ToolCallStatusChangedEvent``，并把
          ``ToolMessage`` 写回 ``RuntimeContextManager``；
        - 取消分支经 ``RuntimeOperations.cancel_run_if_running`` 落定取消终态；
        - 错误上限分支经 ``fail_run_if_running`` 标记 run 失败终态；
        - 阶段二将在此接入 LLM 观察推理并写入明确的观察事实，不在此写消息通道。
    """
    rc = _runtime_config()  # 取运行时配置（含 operations / langfuse_trace_id）
    operations = rc.operations  # 领域操作
    # tools 节点产出的结果摘要
    summaries: list[dict[str, Any]] = state.last_tool_results["observations"]
    instruction = state.last_tool_results["instruction"]
    task_id = operations.get_current_task().id
    run_id = operations.get_current_run().id
    step_id = f"step-{state.step_count}"  # 工具是 model 步的延续，复用同一步 step_id

    expected_call_ids = {
        str(call_id)
        for call_id in state.last_tool_results.get("expected_call_ids", [])
        if call_id
    }
    if expected_call_ids:
        accepted_summaries: list[dict[str, Any]] = []
        observed_call_ids: set[str] = set()
        unexpected_call_ids: set[str] = set()
        duplicate_call_ids: set[str] = set()
        for summary in summaries:
            call_id = str(summary.get("call_id") or "")
            if call_id not in expected_call_ids:
                unexpected_call_ids.add(call_id or "<empty>")
                continue
            if call_id in observed_call_ids:
                duplicate_call_ids.add(call_id)
                continue
            observed_call_ids.add(call_id)
            accepted_summaries.append(summary)
        if unexpected_call_ids or duplicate_call_ids:
            log.error(
                "observe_node_invalid_tool_result_ids",
                extra={
                    "msg": "工具观察结果包含多余或重复的 tool_call_id，已丢弃异常结果",
                    "data": {
                        "step_id": step_id,
                        "unexpected_count": len(unexpected_call_ids),
                        "duplicate_count": len(duplicate_call_ids),
                    },
                },
            )
        summaries = accepted_summaries

    # 1. 结果分发（先于取消判断）：终态事件 + 模型上下文 + 连续失败计数重算。
    # 取消场景工具已执行完毕，结果必须先到达前端与上下文，再落取消终态。
    dispatch = dispatch_tool_observations(
        summaries,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        inherited_error_count=state.tool_error_count,
    )
    tool_error_count = dispatch["tool_error_count"]

    observed_call_ids = {
        str(summary["call_id"])
        for summary in summaries
        if summary.get("call_id")
    }
    missing_call_ids = expected_call_ids - observed_call_ids
    if missing_call_ids:
        # 工具执行层理论上为每个 approved call 返回 observation；若异常丢失结果，先补齐
        # ToolMessage 协议占位，再允许延迟 SystemMessage 入上下文，避免仍有悬空调用。
        log.error(
            "observe_node_missing_tool_results",
            extra={
                "msg": "工具观察结果缺少已批准的工具调用，先补齐协议占位",
                "data": {
                    "step_id": step_id,
                    "expected_count": len(expected_call_ids),
                    "observed_count": len(observed_call_ids),
                    "missing_count": len(missing_call_ids),
                },
            },
        )
        _runtime_context().load_message()

    # 2. 执行后取消判断：工具已执行完毕（结果已分发、上下文配对已闭合），若 run
    # 取消则不再多做一次推理并置取消终态，不进错误上限判定。
    if operations.is_current_run_cancelled():
        log.info(
            "observe_node_cancelled_after_execution",
            extra={
                "msg": (
                    "工具结果观察时检测到 run 已取消，停止后续模型调用，"
                    f"step_id={step_id}"
                ),
                "data": {
                    "step_id": step_id,
                    "tool_error_count": tool_error_count,
                },
            },
        )
        # 取消终态经 RuntimeOperations 条件落定，直接结束。
        operations.cancel_run_if_running(
            end_reason="run_cancelled_after_execution", usage_stats=rc.usage_stats
        )
        return {
            "tool_error_count": tool_error_count,
            "terminal": True,
            "deferred_repair_message": "",
        }

    # model 节点在同一轮发现「合法 + 可修复非法」调用时，只能把修复提示延迟到这里。
    # dispatch 已经按原调用顺序写完全部 ToolMessage，此处追加 SystemMessage 后，消息序列
    # 始终是 AIMessage(tool_calls) -> ToolMessage × N -> SystemMessage。
    deferred_repair_message = state.deferred_repair_message
    if deferred_repair_message:
        if not summaries:
            # 正常 tools 节点会为每个 approved call 产出观察；空批次属于异常恢复路径。
            # 先让 RuntimeContextManager 闭合悬空 tool call，再追加修复提示，避免把非法
            # SystemMessage 插到仍未闭合的 AIMessage(tool_calls) 后面。
            log.error(
                "observe_node_deferred_repair_without_results",
                extra={
                    "msg": "延迟修复提示缺少工具结果，先补齐悬空工具调用占位",
                    "data": {
                        "step_id": step_id,
                        "repair_message_length": len(deferred_repair_message),
                    },
                },
            )
            _runtime_context().load_message()
        _runtime_context().add_message(
            SystemMessage(
                content=deferred_repair_message,
                additional_kwargs={
                    "cosir_message_kind": TOOL_CALL_REPAIR_MESSAGE_KIND,
                },
            )
        )
        log.warning(
            "observe_node_deferred_repair_appended",
            extra={
                "msg": "工具结果之后已追加延迟修复提示",
                "data": {
                    "step_id": step_id,
                    "result_count": len(summaries),
                    "repair_message_length": len(deferred_repair_message),
                },
            },
        )

    if not summaries:
        # 无本批工具结果：不计数也不判定，避免对无新结果时误发 RUN_FAILED。
        # 「连续失败计数滞留」是有意为之——本批没有任何 success/error 信号，既无法证明
        # 连续失败在延续，也无法证明已中断；无信息即不改写，把继承的 tool_error_count
        # 原样保留，等下一批有实际结果时再按信号重置/累加。
        log.info(
            "observe_node_no_results",
            extra={
                "msg": (
                    f"无本批工具结果，跳过观察判定，保留继承计数，"
                    f"step_id={step_id}"
                ),
                "data": {
                    "step_id": step_id,
                    "tool_error_count": state.tool_error_count,
                },
            },
        )
        return {"deferred_repair_message": ""}

    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(summaries),
                "error_count": dispatch["error_count"],
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Settings.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        failed_run = operations.fail_run_if_running(
            end_reason="tool_error_limit_reached", usage_stats=rc.usage_stats
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
                "terminal": True,
                "deferred_repair_message": "",
            }
        log.warning(
            "observe_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id={step_id}",
                "data": {
                    "step_id": step_id,
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
            "deferred_repair_message": "",
        }

    # 正常返回：把更新后的计数写回 state。
    return {"tool_error_count": tool_error_count, "deferred_repair_message": ""}
