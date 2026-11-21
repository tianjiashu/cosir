"""执行当前模型批次的工具调用；结果写入归 observe 节点，挂起判定归 prepare_input。

调用按顺序逐个执行，不做执行层并发：一次回复带多个调用省下的是模型往返，与执行层是否并发无关；
能并发的调用大多是毫秒级的本地读取，或已在工具内部并发（``web_fetch`` 的多个 URL）。每个调用经
``asyncio.to_thread`` 在工作线程执行（复制 contextvars，继承 Run 的日志上下文）：同步阻塞的 handler
若占住事件循环，HTTP、SSE 与「停止」请求都会无响应。

每个调用自己投影生命周期：通过准入时由执行器发 ``running``，结束（含执行链异常、被门禁拒绝）时这里
立即发终态，所以排队中的调用不会先显示为运行中。

续跑与唤醒都从模型边界开始，不自动重放工具。工具执行的外部副作用不能随 context 编辑或取消回滚。
"""

import asyncio
from pathlib import Path
from typing import Any

from app.config.configuration import get_tool_executor, get_tool_registry
from app.config.logging.logger import log
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.tool.schemas.request_tools import RequestTools
from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.node_helper.common import _message_context, _runtime_context, emit
from app.models.enums.error_kind import ErrorKind
from app.models.enums.react_route import ReactRoute


async def _tools_node(state: ReactExecutionState) -> dict[str, Any]:
    """按顺序执行当前模型批次的调用，观察并入批次副本后流转到 observe 节点。

    参数串原样交给执行器，解码与校验归 ``ToolAccessGate``，畸形参数在那里形成错误观察；单个调用的执行链
    异常收口为它的 error 观察。canonical tool 消息由 observe 节点结算，是否挂起由 prepare_input 判定。

    需要用户决定的工具（``ToolDefinition.user_decision``）只能单独调用：与其他调用同批时整批不执行，由模型
    拆开重发——否则批准一个、同批其余已执行或仍在等待的状态无法收敛。

    协程被取消时已在线程里运行的工具不会被强杀（取消由工具自己轮询信号），之后的调用不再执行。事件循环
    在这里取：工作线程里取不到运行中的循环，而启动子 Run、投递实时输出都要回到它。
    """
    rc = _runtime_context()
    batch = state.batch
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    calls = [
        ToolCall(tool_name=call["function"]["name"], arguments=call["function"]["arguments"], call_id=call["id"])
        for call in batch.calls
    ]
    log.info(
        "tools_node_started",
        extra={
            "msg": f"准备执行 {len(calls)} 个工具调用，step_id={step_id}",
            "data": {"step_id": step_id, "call_ids": [call.call_id for call in calls]},
        },
    )
    hitl_names = sorted({call.tool_name for call in calls if _requires_user_decision(call)})
    if hitl_names and len(calls) > 1:
        observations = _reject_mixed_decision_batch(rc, calls, step_id, hitl_names)
    else:
        loop = asyncio.get_running_loop()
        # 准入与发出这次请求时的工具集同源；整批共用一份，批内调用的结果不改变本批准入。
        request_tools = rc.request_tools(_message_context())
        observations = [
            await asyncio.to_thread(_execute_call, rc, call, step_id, loop, request_tools)
            for call in calls
        ]
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具批次处理完成，step_id={step_id}",
            # 只记结局与正文长度：完整结果在 context 表里，日志不脱敏，不该放进工具读到的文件或接口响应。
            "data": {
                "step_id": step_id,
                "results": [
                    {"tool_call_id": o.tool_call_id, "status": o.status, "content_chars": len(o.content or "")}
                    for o in observations
                ],
            },
        },
    )
    return {"batch": batch.model_copy(update={"observations": observations}), "next_node": ReactRoute.OBSERVE}


def _requires_user_decision(call: ToolCall) -> bool:
    definition = get_tool_registry().get_tool_definition(call.tool_name)
    return definition is not None and definition.user_decision


def _reject_mixed_decision_batch(
    rc: RuntimeContext, calls: list[ToolCall], step_id: str, hitl_names: list[str]
) -> list[ToolObservation]:
    log.warning(
        "tool_batch_rejected_mixed_user_decision",
        extra={
            "msg": "需要用户决定的工具未单独调用，整批拒绝执行",
            "data": {
                "session_id": rc.session.id,
                "run_id": rc.run.id,
                "step_id": step_id,
                "hitl_tools": hitl_names,
                "call_ids": [call.call_id for call in calls],
            },
        },
    )
    content = (
        f"{', '.join(hitl_names)} requires the user's decision and must be the only tool call in its "
        "response; none of the tool calls in this response were executed. Call it alone, and issue "
        "the other tool calls in a separate response."
    )
    observations = [tool_error(call.tool_name, content, call.call_id) for call in calls]
    for observation in observations:
        _emit_terminal(rc, observation, step_id)
    return observations


def _execute_call(
    rc: RuntimeContext,
    call: ToolCall,
    step_id: str,
    loop: asyncio.AbstractEventLoop,
    request_tools: RequestTools,
) -> ToolObservation:
    """执行单个调用并发终态；未知工具名也交给执行器，由准入门禁产出稳定观察。

    每个调用各建一份不可变执行上下文：Run / step 事实取自 ``rc``，调用身份只取自 ``call``。
    """
    execution_context = ToolExecutionContext(
        session_id=rc.session.id,
        workspace_id=rc.workspace.id,
        workspace_root=Path(rc.workspace.root_path),
        step_id=step_id,
        run_id=rc.run.id,
        agent=rc.agent,
        event_loop=loop,
        event_sink=rc.event_sink,
        tool_call_id=call.call_id,
        writable_subpath=rc.session.extra.writable_root,
        request_tools=request_tools,
    )
    try:
        observation = get_tool_executor().execute(
            call, execution_context=execution_context, request_tools=request_tools
        )
    except Exception as exc:
        observation = _internal_error(execution_context, call, exc)
    _emit_terminal(rc, observation, step_id)
    return observation


def _emit_terminal(rc: RuntimeContext, observation: ToolObservation, step_id: str) -> None:
    emit(
        terminal_event(
            observation,
            session_id=rc.session.id,
            run_id=rc.run.id,
            step_id=step_id,
            tool_call_id=observation.tool_call_id,
        )
    )


def _internal_error(
    execution_context: ToolExecutionContext, call: ToolCall, exc: Exception
) -> ToolObservation:
    """执行链自身异常（handler 异常已由 runner 归一化）：副作用未知，引导模型先核实状态而不是重试。"""
    log.error(
        "tool_call_internal_error",
        extra={
            "msg": "工具调用执行链内部异常，已收口为 error 观察",
            "data": {
                "error_kind": ErrorKind.RUNTIME_FAILED.value,
                "tool_name": call.tool_name,
                "tool_call_id": call.call_id,
                "session_id": execution_context.session_id,
                "step_id": execution_context.step_id,
                "error": str(exc),
            },
        },
        exc_info=True,
    )
    return tool_error(
        call.tool_name,
        f"internal execution error in the tool pipeline: {type(exc).__name__}. This came from the agent "
        "runtime, not from your arguments, so changing them will not help. The tool may or may not have "
        "run: if it changes state (files, commands, child agents), check the current state with a "
        "read-only tool before acting again. If it happens again, stop using this tool and tell the user.",
        call.call_id,
    )
