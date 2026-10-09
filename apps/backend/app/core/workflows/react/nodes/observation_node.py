"""ReAct-like 工作流的观察节点（``_observe_node``）。

本模块承载「观察节点」的单一职责，作为「工具结果观察处理」的**单一收口**：
``tools`` 节点执行完工具批次后，本节点按固定时序处理本批观察：

1. **结果分发**：经 ``ToolCallLifecycleManager.settle_batch`` 把已治理观察分发到前端事件流
   （``ToolCallStatusChangedEvent`` 终态）与模型上下文（``ToolMessage`` 配对闭合），
   同时重算连续失败计数；命中 ``blocked_calls`` 的观察只写 ``ToolMessage``（隐藏闭合）；
2. **错误计数与上限判定**：连续失败计数达到 ``Constant.Workflow.TOOL_ERROR_LIMIT`` 时经
   ``WorkflowOperations`` 标记失败终态；否则写回计数，让 graph 经条件边回到 ``model``
   节点继续推理。

设计动机（与阶段二演进对齐）：
- 「观察工具结果」是独立于「执行工具」的推理步骤。阶段一做确定性的分发、错误计数与
  上限判定；阶段二将在此基础上接入 LLM，对摘要中的 ``content`` / ``error`` / ``reason``
  做判断（例如识别「工具结果是否回答了模型的问题」），产出路由信号。把这一步独立成
  graph node，使其可独立计步、独立重试/取消、独立产事件——本节点即为阶段二
  「LLM 观察工具结果」的扩展点。
- 本节点只读 ``tools`` 节点产出的摘要/观察做分发与判定，不再补落库与配对闭合
  之外的状态写入；即使观察节点崩溃，run 终态路径上的 ``ToolCallsSettledEvent``
  仍会兜底收束未决 tool-call part。
"""

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.react.node_helper.common import _runtime_config
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState


async def _observe_node(state: ReactGraphState) -> dict:
    """工具结果观察节点：终态事件分发 + 模型上下文写回 + 非法调用结算 + 错误上限判定。

    本节点是「工具结果观察处理」的单一收口，按固定时序处理：

    1. **结果分发**：对本批 ``last_tool_results`` 的观察逐条发出
       ``ToolCallStatusChangedEvent`` 终态事件（``completed`` / ``failed`` /
       ``cancelled``）并写回模型上下文（``ToolMessage``，闭合
       ``AIMessage.tool_calls`` 配对），同时重算连续失败计数（``completed`` 清零、
       ``failed`` 且 ``retryable is False`` 时累加、``cancelled`` 不计）。命中
       ``blocked_calls`` 的观察只写 ``ToolMessage``（前端无 part，不发终态事件）。
    2. **空结果批次**：本批无观察时循环不执行，不计数也不判定，保留继承的
       ``tool_error_count``（无信息即不改写），避免无新结果时误发 RUN_FAILED。
    3. **错误上限判定**：连续失败计数达到 ``Constant.Workflow.TOOL_ERROR_LIMIT`` 时经
       ``WorkflowOperations.fail_run_if_running`` 标记失败终态；否则写回计数，
       让 graph 经条件边回到 ``model`` 节点。

    参数:
        state: 当前 graph state，含本批 ``last_tool_results``、继承的 ``tool_error_count``。

    返回:
        需要合并回 graph state 的增量。所有分支均回写动态 ``next_node``、工具调用生命周期，并清空
        已结算的本批摘要 ``last_tool_results``；路由值为 ``model`` 或 ``end``（等待用户输入由
        ``wait_user`` 节点在进入本节点之前处理）。

        清空 ``last_tool_results`` 是**批次边界**：``tools`` 节点恒定按 ``tool_call_id`` 合并
        新观察进本批摘要（同一模型步可能分多遍执行），若本节点不清理，上一批的观察会被下一批
        连带重新结算——而 ``settle_batch`` 依摘要重算连续失败计数，陈旧的成功记录会把计数清零
        （掩盖真实的连续失败），陈旧的失败记录会反复累加（无新失败也可能被判定达到错误上限）。
        待决请求不需要在这里清算：它由观察上的声明表达，随本批摘要一并消失。

    异常:
        RuntimeError: ``state.tool_call_lifecycle`` 缺失（应由 ``model`` 节点写入）。
        KeyError: 观察摘要缺少模型上下文所需字段（上游契约错误）。

    副作用:
        - 分发阶段经 stream writer 发出 ``ToolCallStatusChangedEvent``，并把
          ``ToolMessage`` 写回 ``RuntimeContextManager``（命中 ``blocked_calls`` 的观察只写
          ``ToolMessage``，不发终态事件）；
        - 错误上限分支经 ``fail_run_if_running`` 标记 run 失败终态；
        - 阶段二将在此接入 LLM 观察推理并写入明确的观察事实，不在此写消息通道。
    """
    rc = _runtime_config()  # 取运行时配置（本节点只用其中的 operations 与 usage_stats）
    operations = rc.operations  # 领域操作
    # tools 节点产出的结果摘要。首次运行（从未执行过工具步）时 state 里仍是初始空 dict，
    # 故用 ``get`` 防御，避免任何路径漏置键时整条收口链路崩溃。
    observations = state.last_tool_results.get("observations", [])
    instruction = state.last_tool_results.get("instruction", "")
    task_id = operations.get_current_task().id
    run_id = operations.get_current_run().id
    step_id = f"step-{state.step_count}"  # 工具是 model 步的延续，复用同一步 step_id
    lifecycle = state.tool_call_lifecycle
    if lifecycle is None:
        raise RuntimeError("tool_call_lifecycle is required before observing tool results")
    # 1. 结果分发：终态事件 + 模型上下文 + 连续失败计数重算。
    dispatch = lifecycle.settle_batch(
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        summaries=observations,
        inherited_error_count=state.tool_error_count,
    )
    lifecycle = dispatch.lifecycle
    tool_error_count = dispatch.tool_error_count

    log.info(
        "observe_node_completed",
        extra={
            "msg": f"工具结果观察完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "result_count": len(observations),
                "error_count": dispatch.error_count,
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Constant.Workflow.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        failed_run = operations.fail_run_if_running(
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
                "tool_call_lifecycle": lifecycle,
                "next_node": ReactRoute.END,
                "last_tool_results": {},
            }
        log.warning(
            "observe_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_error_count": tool_error_count,
                    "limit": Constant.Workflow.TOOL_ERROR_LIMIT,
                    "instruction": instruction,
                },
            },
        )
        # 失败终态：错误上限时经 canonical writer 落定失败。
        return {
            "tool_error_count": tool_error_count,
            "tool_call_lifecycle": lifecycle,
            "next_node": ReactRoute.END,
            "last_tool_results": {},
        }

    # 正常返回：把更新后的计数与 lifecycle 写回 state，并清空已结算的本批摘要——本节点是
    # 「工具结果观察处理」的单一收口，也是批次摘要的唯一清算点：下一模型步不应再看到上一批的观察
    # （否则会被连带重新结算，见上文批次边界说明）。
    return {
        "tool_error_count": tool_error_count,
        "tool_call_lifecycle": lifecycle,
        "next_node": ReactRoute.MODEL,
        "last_tool_results": {},
    }
