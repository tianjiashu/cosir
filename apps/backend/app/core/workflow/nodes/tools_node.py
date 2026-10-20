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
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.common import _runtime_context, get_stream_writer
from app.models.runtime_notice import RuntimeNotice
from app.models.user_decision import UserDecision


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
    """解析并执行当前模型批次；批准恢复时批次只含目标调用。

    多调用批次若含 HIL 工具则整体拒绝，防止其它工具先产生副作用；待决请求必须独占批次。
    参数 JSON
    在此转换为 dict；畸形参数直接形成观察，不进入工具执行器。结果仅合并到本批摘要，
    canonical tool 消息与终态事件统一由 observe 节点结算。
    """
    rc = _runtime_context()  # 取运行时配置
    operations = rc.operations  # 领域操作
    batch = rc.batch
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    instruction = batch.instruction

    # 普通续跑从模型边界开始；等待批准只恢复待批准的单条调用，因此无需第二套
    # pending/running 快照防止节点重放。
    prepared = [_to_tool_call(call, batch.decision_for(call["id"])) for call in batch.calls]
    tool_calls = [call for call in prepared if isinstance(call, ToolCall)]
    invalid_observations = [item for item in prepared if isinstance(item, ToolObservation)]

    definitions_by_name = {definition.name: definition for definition in operations.all_vaild_tools}
    hil_tool_names = sorted(
        {
            raw_call["function"]["name"]
            for raw_call in batch.calls
            if (definition := definitions_by_name.get(raw_call["function"]["name"])) is not None
            and definition.need_HIL
        }
    )
    blocked_batch = len(batch.calls) > 1 and bool(hil_tool_names)

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
            for call in batch.calls
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
                run_id=operations.get_current_run().id,
            )
        )
        next_node = ReactRoute.OBSERVE
        log.warning(
            "tools_node_hil_batch_blocked",
            extra={
                "msg": "包含人工确认工具的多调用批次已整体拦截，未执行任何工具",
                "data": {
                    "run_id": operations.get_current_run().id,
                    "step_id": step_id,
                    "hil_tool_names": hil_tool_names,
                    "call_ids": [call.call_id for call in tool_calls],
                },
            },
        )
    else:
        for tool_call in tool_calls:
            if not batch.was_projected(tool_call.call_id):
                continue
            get_stream_writer()(
                ToolCallStatusChangedEvent(
                    task_id=task_id,
                    run_id=operations.get_current_run().id,
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
        requires_input = any(
            observation.user_input_request is not None for observation in observations
        )
        if requires_input and len(batch.calls) != 1:
            raise ValueError("待决请求必须独占工具批次；产出请求的工具应声明 need_HIL")
        next_node = ReactRoute.WAIT_USER if requires_input else ReactRoute.OBSERVE

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

    batch.merge_observations(observation_dicts)
    batch.approved_call = None
    return {"next_node": next_node}
