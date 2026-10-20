"""执行当前批次尚未开始的工具，逐条持久化结果并投影。

等待恢复只重新打开用户批准的调用；普通续跑从模型边界开始，不自动重放工具。
工具执行的外部副作用不能随 context 编辑或取消回滚。"""

import asyncio
import copy
import dataclasses
from typing import Any

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam

from app.assistant_transport.event import ToolCallStatusChangedEvent
from app.config.logging.logger import log
from app.core.llm_provider.message_codec import decode_tool_arguments
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.tools.schemas.user_decision import UserDecision
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import _runtime_context, get_stream_writer
from app.models.runtime_notice import RuntimeNotice


def _merge_batch_results(
    previous: dict[str, Any],
    incoming: list[dict[str, Any]],
    *,
    instruction: str,
) -> dict[str, Any]:
    """把本批观察并入既有批次，按 ``tool_call_id`` 覆盖同一条调用的观察。

    为什么必须合并而不是整批覆盖：同一模型步的调用可能分多遍执行（用户批准后重跑被批准的调用），
    整批覆盖会丢掉同批其它调用的结果，``observe`` 结算时就会少写 ``tool 消息``（模型侧的配对
    出现空洞）。批次以 ``observe`` 的结算为界（结算后清空 ``last_tool_results``），因此首遍合并的
    起点是空批次，与「覆盖」等价。

    ``instruction`` 由调用方显式给出，不从 ``previous`` 读取：批次起点为空时 ``previous`` 里没有
    instruction，从它读取会把模型本步的伴随文本丢掉。

    参数:
        previous: 既有 ``last_tool_results``（``instruction`` / ``observations``）。
        incoming: 本遍执行产出的观察摘要列表。
        instruction: 本模型步的 instruction（``state.instruction``），每遍执行都传同一个值。

    返回:
        合并后的新 dict：``instruction`` 为入参值，``observations`` 按入参顺序用新观察覆盖同
        ``tool_call_id`` 的旧项，新调用追加在尾部。

    异常:
        无（缺失 ``tool_call_id`` 的观察原样追加，交由下游契约校验）。

    副作用:
        无（纯函数，不修改入参）。
    """

    existing = previous.get("observations")
    merged: list[dict[str, Any]] = list(existing) if isinstance(existing, list) else []
    index = {
        str(item.get("tool_call_id") or ""): position
        for position, item in enumerate(merged)
        if isinstance(item, dict) and item.get("tool_call_id")
    }
    for observation in incoming:
        call_id = str(observation.get("tool_call_id") or "")
        position = index.get(call_id)
        if position is None:
            index[call_id] = len(merged)
            merged.append(observation)
            continue
        merged[position] = observation
    return {
        "instruction": instruction,
        "observations": merged,
    }


def _to_tool_call(
    raw: ChatCompletionMessageFunctionToolCallParam, decision: UserDecision | None = None
) -> ToolCall | ToolObservation:
    """在模型与工具边界解析 JSON；畸形参数形成可配对观察，不进入执行器。"""
    name = raw["function"]["name"]
    call_id = raw["id"]
    try:
        arguments = decode_tool_arguments(raw["function"]["arguments"])
    except ValueError:
        return tool_error(
            tool_name=name,
            error="Invalid tool arguments",
            reason="Tool arguments must be a valid JSON object; correct them before retrying.",
            retryable=True,
            tool_call_id=call_id,
        )
    return ToolCall(
        tool_name=name,
        call_id=call_id,
        arguments=arguments,
        user_decision=decision,
    )


def _summary(observation: ToolObservation) -> dict[str, Any]:
    """观察摘要只保留模型结算与等待恢复所需字段，内部产物不进入 Run。"""
    result = dataclasses.asdict(observation)
    result.pop("artifact_data", None)
    return result


async def _tools_node(state: ReactExecutionState) -> dict[str, Any]:
    """解析并执行当前模型批次；批准恢复时 state.calls 只含目标调用。

    多调用批次若含 HIL 工具则整体拒绝，防止其它工具先产生副作用。参数 JSON
    在此转换为 dict；畸形参数直接形成观察，不进入工具执行器。结果仅合并到本批摘要，
    canonical tool 消息与终态事件统一由 observe 节点结算。
    """
    rc = _runtime_context()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    instruction = state.instruction

    # 普通续跑从模型边界开始；等待批准只恢复待批准的单条调用，因此无需第二套
    # pending/running 快照防止节点重放。
    prepared = [_to_tool_call(call, state.replay_decision) for call in state.calls]
    tool_calls = [call for call in prepared if isinstance(call, ToolCall)]
    invalid_observations = [item for item in prepared if isinstance(item, ToolObservation)]

    definitions_by_name = {definition.name: definition for definition in operations.all_vaild_tools}
    hil_tool_names = sorted(
        {
            raw_call["function"]["name"]
            for raw_call in state.calls
            if (definition := definitions_by_name.get(raw_call["function"]["name"])) is not None
            and definition.need_HIL
        }
    )
    blocked_batch = len(state.calls) > 1 and bool(hil_tool_names)

    log.info(
        "tools_node_started",
        extra={
            "msg": f"准备执行 {len(tool_calls)} 个工具调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "call_count": len(tool_calls),
                "call_ids": [call.call_id for call in tool_calls],
                "instruction": instruction,
            },
        },
    )

    if blocked_batch:
        # 整批拒绝，避免并行调用中的普通工具先产生副作用、HIL 工具再等待用户确认。
        observations = [
            tool_error(
                tool_name=call["function"]["name"],
                error="error",
                reason="blocked_batch",
                retryable=True,
                tool_call_id=call["id"],
            )
            for call in state.calls
        ]
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        task_runtime_spaces.get_or_create(task_id).defer_runtime_notice(
            RuntimeNotice(
                content=(
                    f"The tool(s) {', '.join(hil_tool_names)} require human approval and can "
                    "only be called alone in a tool batch. The previous batch was blocked and "
                    "none of its tools ran. Retry the human approval tool in a separate batch."
                ),
                kind="tool_batch_blocked",
                run_id=rc.run.id,
            )
        )
        next_node = ReactRoute.OBSERVE
        log.warning(
            "tools_node_hil_batch_blocked",
            extra={
                "msg": "包含人工确认工具的多调用批次已整体拦截，未执行任何工具",
                "data": {
                    "run_id": rc.run.id,
                    "step_id": step_id,
                    "hil_tool_names": hil_tool_names,
                    "call_ids": [call.call_id for call in tool_calls],
                },
            },
        )
    else:
        for tool_call in tool_calls:
            if tool_call.call_id not in rc.step_context.projected_tool_calls:
                continue
            get_stream_writer()(
                ToolCallStatusChangedEvent(
                    task_id=task_id,
                    run_id=rc.run.id,
                    step_id=step_id,
                    tool_call_id=tool_call.call_id,
                    status="running",
                    args=copy.deepcopy(tool_call.arguments)
                    if tool_call.arguments is not None
                    else None,
                )
            )
        tool_run = (
            await operations.run_tool_calls(
                task_id, tool_calls, step_id, asyncio.get_running_loop()
            )
            if tool_calls
            else None
        )
        observations = invalid_observations + (tool_run.observations if tool_run else [])
        next_node = (
            ReactRoute.WAIT_USER
            if any(observation.user_input_request is not None for observation in observations)
            else ReactRoute.OBSERVE
        )

    log.info(
        "tools_node_tool_run",
        extra={
            "msg": f"工具批次执行结果，step_id={step_id}",
            "data": {
                "blocked_batch": blocked_batch,
                "observations": [_summary(observation) for observation in observations],
            },
        },
    )

    # 终态与模型上下文写回统一由 observe 节点处理。
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具批次处理完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(observations),
            },
        },
    )

    observation_dicts = [_summary(observation) for observation in observations]

    return {
        # 恒定按 ``tool_call_id`` 合并：同一模型步可能分多遍执行（批准后重跑被批准的调用），
        # 整批覆盖会丢掉同批其它调用的结果，observe 结算时就会少写 tool 消息（模型侧配对
        # 出现空洞）。批次边界由 observe 的结算（清空 last_tool_results）划定，故首遍合并的
        # 起点是空批次，与「覆盖」等价。
        "last_tool_results": _merge_batch_results(
            state.last_tool_results,
            observation_dicts,
            instruction=instruction or "",
        ),
        "next_node": next_node,
    }
