"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：审批恢复后的工具执行与运行中的 running 事件。
工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实经明确的开始/完成回调写入
canonical conversation state。本节点**不再**分发终态事件（``completed`` /
``failed`` / ``cancelled``）、不再写回模型上下文、不做错误计数/上限判定——
全部收敛到 ``observe`` 节点做「工具结果观察处理」的单一收口（见
``observation_node`` 与 ``tool_observation_dispatcher``）。

本节点产出经 ``tool_observation_summary`` 治理的可序列化摘要（``last_tool_results``）
供 ``observe`` 消费；执行前取消分支（工具尚未执行、无结果可观察）仍保留在本节点，
仅收口取消事件并落定取消终态；模型协议占位闭合统一下沉到
``RuntimeContextManager.load_message`` 在下次取数时自动补 ``ToolMessage`` 占位，
本节点不再补/落库占位。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses

from langgraph.config import get_stream_writer

from app.config.logging.logger import log
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall
from app.core.workflows.nodes.helper.common import (
    _runtime_config,
)
from app.core.workflows.nodes.helper.tool_observation_summary import (
    build_tool_result_summaries,
)

from ..event import ToolCallStatusChangedEvent
from ..react.state import ReactGraphState


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：执行工具并产出结果摘要供 observe 观察。

    工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实经明确的开始/完成回调写入
    canonical state。本节点只负责「执行前取消检查 + running 事件 + 执行 + 产出摘要」；
    终态事件（``completed`` / ``failed`` / ``cancelled``）、模型上下文写回、错误计数
    与上限判定全部下沉到 ``observe`` 节点。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求。生命周期回调在协程内
    构造后闭包捕获，并随工具执行传入工作线程，不依赖 LangGraph stream writer。

    参数:
        state: 当前 graph state，含待执行工具调用。

    返回:
        需要合并回 graph state 的增量：正常分支 ``last_tool_results`` 为本批次工具结果
        治理摘要（经 ``build_tool_result_summaries`` 脱敏、截断并携带预算后的 UI data，
        可落 checkpoint，供 ``observe`` 节点分发与判定），并清空 ``pending_tool_calls``；
        执行前取消分支置 ``terminal=True`` 且返回空摘要——因为 ``_after_tools`` 在
        ``terminal`` 时直接 END、不进 observe，返回摘要既无人消费又会撑大 checkpoint。

    副作用:
        - 经 stream writer 发出本批 ``running`` 状态事件（执行前取消分支为
          ``cancelled`` 事件）；终态事件不在本节点发出；
        - 执行前取消分支（工具尚未执行、无结果可观察，不能下沉 observe）置
          ``terminal=True`` 让 graph 走 END；因提前 return 不进入 ``run_tool_calls``，
          模型协议层面的配对闭合统一由 ``RuntimeContextManager.load_message`` 在下次
          取数时自动补 ``ToolMessage`` 占位，本节点不再构造/落库占位消息、亦不越界
          访问 service 受保护成员；
        - 工具生命周期事实经明确回调写入 canonical state；状态写入 **run**。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    tool_calls = state.pending_tool_calls["tool_calls"]  # 来自 model 节点写入的待执行工具调用
    instruction = state.pending_tool_calls["instruction"]
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    run_id = operations.get_current_run().id
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    stream_writer = get_stream_writer()

    approved_dicts = tool_calls
    approved_calls = [ToolCall.from_dict(item) for item in approved_dicts]

    # ★ 取消检查：审批恢复后（或自动放行时）、工具执行前，若 run 已被取消则跳过工具执行。
    # 第零铁律（正确性优先）：本分支提前 return，不进入下方 ``run_tool_calls`` 路径，故
    # ``WorkflowOperations.run_tool_calls`` 的取消兜底（原
    # ``_build_result_with_cancel_placeholders``）已
    # 移除，模型协议配对闭合统一由 ``RuntimeContextManager.load_message`` 收口。上一轮
    # ``_model_node`` 已把 ``AIMessage.tool_calls`` 写入 ``RuntimeContextManager``，会在
    # 下次模型取数时被自动补 ``ToolMessage`` 占位，不会因悬空 ``tool_calls`` 触发协议校验失败；
    # 本分支只负责 canonical 审计终态与收口取消事件，工具执行与生命周期事实写入由 service 承担。
    if operations.is_current_run_cancelled():
        log.info(
            "tools_node_cancelled",
            extra={
                "msg": f"工具节点恢复后检测到 run 已取消，跳过工具执行，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": operations.get_current_run().id},
            },
        )
        for tool_call in approved_calls:
            stream_writer(
                ToolCallStatusChangedEvent(
                    task_id=task_id,
                    run_id=run_id,
                    step_id=step_id,
                    tool_call_id=tool_call.call_id,
                    status="cancelled",
                )
            )

        # 收口取消终态事件：本分支是实际检测到 run 取消的执行点，须发出
        # RUN_CANCELLED 供前端 StatusBadge 渲染；工具尚未执行无 token 累积，
        # 经统一 emit_run_cancelled 构造（携带 langfuse_trace_id，与 model/observe 一致）。
        operations.cancel_run_if_running(end_reason="runtime_cancelled", usage_stats=rc.usage_stats)
        return {
            "pending_tool_calls": {},
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "last_tool_results": {},
        }

    log.info(
        "tools_node_resumed",
        extra={
            "msg": f"准备执行 {len(approved_calls)} 个工具调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "approved_count": len(approved_calls),
                "instruction": instruction,
            },
        },
    )
    for tool_call in approved_calls:
        stream_writer(
            ToolCallStatusChangedEvent(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=tool_call.call_id,
                status="running",
            )
        )
    tool_run: ToolRunResult = await asyncio.to_thread(
        operations.run_tool_calls,
        str(task.id),
        approved_calls,
        step_id,
        running_loop=asyncio.get_running_loop(),
    )
    log.info(
        "tools_node_tool_run",
        extra={
            "msg": f"工具批次执行结果，step_id={step_id}",
            "data": {"tool_run": dataclasses.asdict(tool_run)},
        },
    )
    observations = tool_run.observations  # 每个工具调用的观察结果
    # 终态事件（completed/failed/cancelled）、模型上下文写回与错误计数统一收敛到
    # observe 节点（tool_observation_dispatcher），本节点只产出治理摘要。

    success_count = sum(1 for o in observations if o.status == "success")
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具执行完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(observations),
                "success_count": success_count,
                "error_count": len(observations) - success_count,
            },
        },
    )

    return {
        "pending_tool_calls": {},  # 清空待执行工具调用
        "last_tool_results": build_tool_result_summaries(observations, instruction),
    }
