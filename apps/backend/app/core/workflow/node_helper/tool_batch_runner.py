"""执行一批模型请求的工具调用：串行调用逐个执行，声明为可并行的调用并发执行。

每个调用自己投影生命周期：通过准入、即将交给 handler 时发 ``running``，结束（含执行链异常）
时立即发终态，所以排队中的串行调用不会先显示为运行中，并行组里先跑完的调用先变色。只投影
本步创建过工具卡片的调用（``was_projected``）。

两条路径都不在事件循环线程上同步等待：同步阻塞的 thread 模式 handler（大仓库搜索、长时间
``delegate_task``）若占住事件循环，HTTP、SSE 与「停止」请求都会无响应。并行组使用本批专属的
线程池，而不是默认执行器：长工具不会占满 ``asyncio.to_thread`` 共用的线程，拖慢数据库读取。
两条路径都复制当前 contextvars 提交，工具线程因此继承父 Run 的日志上下文与运行作用域。

需要用户决定的工具（``ToolDefinition.user_decision`` 非 ``None``）只能单独调用：与其他调用混在
一批时整批在执行前报错，任何调用都不执行，由模型拆开重发——否则批准一个、同批其余已执行或仍在
等待的状态无法收敛。用户决定后的再次执行（批准或驳回）也走这里，决定经 ``decision`` 交给 handler。
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.config.configuration import get_tool_executor, get_tool_registry
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.schemas import ToolCall, ToolExecutionContext, ToolObservation
from app.core.tool.results.tool_error import tool_error
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.inbox.task_message import DecisionBody
from app.core.workflow.node_helper.common import emit
from app.models.enums.error_kind import ErrorKind


async def run_tool_batch(
        rc: RuntimeContext,
        calls: list[ToolCall],
        step_id: str,
        batch: ToolBatch,
        decision: DecisionBody | None = None,
) -> list[ToolObservation]:
    """执行一批工具调用，返回与每个调用一一对应的观察：先串行组（入参顺序），再并行组（入参顺序）。

    单个调用的执行链路异常收口为该调用的 error 观察，本函数不抛出。协程被取消时，已在线程里
    运行的工具不会被强杀（取消由工具自己轮询信号），尚未开始的并行调用随线程池关闭丢弃。

    事件循环必须在这里取：本函数运行在事件循环线程，工具调用在工作线程执行，线程内取不到运行中
    循环，而启动子 Run、投递实时输出都要回到该循环；它随调用传给 ``_execute_call``。
    """
    if len(calls) > 1 and any(_requires_user_decision(call) for call in calls):
        return _reject_mixed_decision_batch(rc, calls, step_id, batch)
    loop = asyncio.get_running_loop()
    serial = [call for call in calls if not _is_parallel(call)]
    parallel = [call for call in calls if _is_parallel(call)]
    observations = [
        await asyncio.to_thread(_execute_call, rc, call, step_id, batch, loop, decision)
        for call in serial
    ]
    if parallel:
        observations.extend(await _run_parallel(rc, parallel, step_id, batch, loop))
    return observations


async def _run_parallel(
        rc: RuntimeContext,
        calls: list[ToolCall],
        step_id: str,
        batch: ToolBatch,
        loop: asyncio.AbstractEventLoop,
) -> list[ToolObservation]:
    pool = ThreadPoolExecutor(
        max_workers=min(len(calls), Constant.Tools.MAX_PARALLEL_CALLS),
        thread_name_prefix="tool-parallel",
    )
    try:
        return list(
            await asyncio.gather(
                *(
                    loop.run_in_executor(
                        pool,
                        partial(
                            contextvars.copy_context().run,
                            _execute_call,
                            rc,
                            call,
                            step_id,
                            batch,
                            loop,
                        ),
                    )
                    for call in calls
                )
            )
        )
    finally:
        # 不在事件循环线程上等 worker 收尾，否则取消路径会被阻塞到工具跑完。
        pool.shutdown(wait=False, cancel_futures=True)


def _is_parallel(call: ToolCall) -> bool:
    definition = get_tool_registry().get_tool_definition(call.tool_name)
    return definition is not None and definition.parallel_mode == "parallel"


def _requires_user_decision(call: ToolCall) -> bool:
    definition = get_tool_registry().get_tool_definition(call.tool_name)
    return definition is not None and definition.user_decision is not None


def _reject_mixed_decision_batch(
        rc: RuntimeContext, calls: list[ToolCall], step_id: str, batch: ToolBatch
) -> list[ToolObservation]:
    """HITL 工具与其他调用（或多个 HITL 调用）同批：每个调用都返回同一条错误观察，一律不执行。"""
    hitl_names = sorted({call.tool_name for call in calls if _requires_user_decision(call)})
    log.warning(
        "tool_batch_rejected_mixed_user_decision",
        extra={
            "msg": "需要用户决定的工具未单独调用，整批拒绝执行",
            "data": {
                "task_id": rc.task.id,
                "run_id": rc.run.id,
                "step_id": step_id,
                "hitl_tools": hitl_names,
                "call_ids": [call.call_id for call in calls],
            },
        },
    )
    observations: list[ToolObservation] = []
    for call in calls:
        observation = tool_error(
            tool_name=call.tool_name,
            content=(
                f"{', '.join(hitl_names)} requires the user's decision and must be the only tool "
                "call in its response; none of the tool calls in this response were executed. "
                "Call the tool that needs the user's decision alone in its own response, and "
                "issue the other tool calls in a separate response."
            ),
            tool_call_id=call.call_id,
        )
        if batch.was_projected(call.call_id):
            emit(
                terminal_event(
                    observation,
                    task_id=rc.task.id,
                    run_id=rc.run.id,
                    step_id=step_id,
                    tool_call_id=call.call_id,
                )
            )
        observations.append(observation)
    return observations


def _execution_context(
        rc: RuntimeContext,
        call: ToolCall,
        step_id: str,
        loop: asyncio.AbstractEventLoop,
        decision: DecisionBody | None = None,
) -> ToolExecutionContext:
    """单次调用的完整执行边界：Run / step 事实取自 ``rc``，调用身份只取自 ``call``。

    每个调用各建一份不可变上下文，同批调用因此不可能共享调用身份；执行器直接信任它，不再特化。
    """
    return ToolExecutionContext(
        task_id=rc.task.id,
        workspace_id=rc.workspace.id,
        workspace_root=Path(rc.workspace.root_path),
        step_id=step_id,
        run_id=rc.run.id,
        agent=rc.agent,
        event_loop=loop,
        event_sink=rc.event_sink,
        tool_call_id=call.call_id,
        user_decision=decision,
    )


def _execute_call(
        rc: RuntimeContext,
        call: ToolCall,
        step_id: str,
        batch: ToolBatch,
        loop: asyncio.AbstractEventLoop,
        decision: DecisionBody | None = None,
) -> ToolObservation:
    """执行单个调用并投影其生命周期；未知工具名也交给执行器，由准入门禁产出稳定观察。"""
    execution_context = _execution_context(rc, call, step_id, loop, decision)

    try:
        observation = get_tool_executor().execute(
            call,
            execution_context=execution_context,
            allowed_tool_names=rc.allows_tools,
        )
    except Exception as exc:
        observation = _internal_error(execution_context, call, exc)
    if batch.was_projected(call.call_id):
        emit(
            terminal_event(
                observation,
                task_id=execution_context.task_id,
                run_id=execution_context.run_id,
                step_id=execution_context.step_id,
                tool_call_id=call.call_id,
            )
        )
    return observation


def _internal_error(
        execution_context: ToolExecutionContext, call: ToolCall, exc: Exception
) -> ToolObservation:
    """执行链自身异常（非 handler 异常，后者已由 runner 归一化）收口为该调用的 error 观察。"""
    log.error(
        "tool_call_internal_error",
        extra={
            "msg": "工具调用执行链内部异常，已收口为 error 观察",
            "data": {
                "error_kind": ErrorKind.RUNTIME_FAILED.value,
                "tool_name": call.tool_name,
                "tool_call_id": call.call_id,
                "task_id": execution_context.task_id,
                "step_id": execution_context.step_id,
                "error": str(exc),
            },
        },
        exc_info=True,
    )
    header = f"internal execution error in the tool pipeline: {type(exc).__name__}"
    return tool_error(
        tool_name=call.tool_name,
        content=(
            f"{header}. "
            "This failure came from the agent runtime, not from your arguments or the tool's own "
            "logic, so changing the arguments will not help. The call may have been interrupted "
            "before or after the tool ran, so its side effects are unknown: if the tool changes "
            "state (files, commands, child agents), verify the current state with a read-only tool "
            "before acting again instead of repeating the call. If the same internal error happens "
            "again, stop using this tool and tell the user that the tool runtime is failing, "
            "including the error above."
        ),
        tool_call_id=call.call_id,
    )
