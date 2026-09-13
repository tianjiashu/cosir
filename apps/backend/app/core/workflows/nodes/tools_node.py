"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：审批恢复后的工具执行与执行前取消。
工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实由
``ToolCallLifecycleManager`` 写入 canonical conversation state。本节点**不再**分发终态事件
（``completed`` /
``failed`` / ``cancelled``）、不再写回模型上下文、不做错误计数/上限判定——
全部收敛到 ``observe`` 节点做「工具结果观察处理」的单一收口（见 ``observation_node`` 与
``tool_call_lifecycle`` 的 ``ToolCallLifecycleManager``）。

本节点产出可序列化摘要（``last_tool_results``，即本批 ``ToolObservation`` 的
``dataclasses.asdict`` 投影，键名与执行层字段一致，含 ``tool_call_id`` /
``display_data``）供 ``observe`` 消费；执行前取消分支（工具尚未执行、无结果可观察）
仍保留在本节点，
仅收口取消事件并落定取消终态；业务恢复 checkpoint 时本节点跳过旧工具批次，随后由
``model_node.load_message`` 统一闭合未完成调用。与模型节点共享的运行时原语见
``common``。
"""

import asyncio
import dataclasses

from app.config.logging.logger import log
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall
from app.core.workflows.nodes.helper.common import _runtime_config

from ..react.state import ReactGraphState


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：执行工具并产出结果摘要供 observe 观察。

    工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实经明确的开始/完成回调写入
    canonical state。本节点只负责「执行前取消检查 + 执行 + 产出摘要」；
    终态事件（``completed`` / ``failed`` / ``cancelled``）、模型上下文写回、错误计数
    与上限判定全部下沉到 ``observe`` 节点。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求。生命周期回调在协程内
    构造后闭包捕获，并随工具执行传入工作线程，不依赖 LangGraph stream writer。

    参数:
        state: 当前 graph state，含待执行工具调用。

    返回:
        需要合并回 graph state 的增量：正常分支 ``last_tool_results`` 为本批次工具观察的
        ``dataclasses.asdict`` 投影（键名即执行层字段名 ``tool_call_id`` /
        ``display_data``，可落 checkpoint，供 ``observe`` 节点分发与判定）；
        本节点从 ``ToolCallLifecycleManager`` 读取 ``running`` 调用执行；无 running 调用时
        直接短路返回；
        业务恢复时跳过 checkpoint 中的旧工具调用，返回空摘要并置 ``terminal=False``，让
        ``observe`` 把 Agent 推回下一轮推理；执行前取消分支置 ``terminal=True`` 且返回空摘要——
        因为 ``_after_tools`` 在
        ``terminal`` 时直接 END、不进 observe，返回摘要既无人消费又会撑大 checkpoint。

    副作用:
        - 执行前取消分支经 ``ToolCallLifecycleManager`` 发出 ``cancelled`` 事件；
          ``running`` 与终态事件不在本节点发出；
        - 执行前取消分支（工具尚未执行、无结果可观察，不能下沉 observe）置
          ``terminal=True`` 让 graph 走 END；因提前 return 不进入 ``run_tool_calls``，
          模型协议层面的配对闭合统一由 ``RuntimeContextManager.load_message`` 在下次
          取数时自动补 ``ToolMessage`` 占位，本节点不再构造/落库占位消息、亦不越界
          访问 service 受保护成员；
        - 工具生命周期事实经明确回调写入 canonical state；状态写入 **run**。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    lifecycle = state.tool_call_lifecycle
    if lifecycle is None:
        raise RuntimeError("tool_call_lifecycle is required before tools_node execution")
    # 仅执行状态为 running 的合法调用；pending（参数非法）调用不执行，由 observe 节点统一结算。
    approved_calls = [
        ToolCall.from_dict(
            {
                "tool_name": record.tool_name,
                "arguments": record.args,
                "call_id": record.tool_call_id,
            }
        )
        for record in lifecycle.calls.values()
        if record.status == "running"
    ]
    instruction = state.instruction
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）

    if not approved_calls:
        # 本轮无 running 调用（仅参数非法 pending）：不执行、不 spawn worker，直接进入 observe
        # 结算非法调用，避免产生空的结果集合与无谓的并发开销。放在取 task 之前，使取消/任务
        # 等运行时查询无需为「无执行」场景付出开销。
        log.info(
            "tools_node_no_runnable_calls",
            extra={
                "msg": f"本轮无 running 工具调用，跳过执行，step_id={step_id}",
                "data": {"step_id": step_id, "lifecycle_size": len(lifecycle.calls)},
            },
        )
        return {
            "tool_call_lifecycle": lifecycle,
            "last_tool_results": {
                "instruction": instruction or "",
                "observations": [],
                "expected_call_ids": [],
            },
        }

    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    run_id = operations.get_current_run().id

    # 取消检查：审批恢复后（或自动放行时）、工具执行前，若 run 已被取消则跳过工具执行。
    # 未完成调用的上下文闭合由下一次 model_node.load_message() 统一兜底；本分支只负责
    # 当前 workflow 已经观察到取消的执行前场景。
    if operations.is_current_run_cancelled():
        log.info(
            "tools_node_cancelled",
            extra={
                "msg": f"工具节点恢复后检测到 run 已取消，跳过工具执行，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": operations.get_current_run().id},
            },
        )
        # 收口取消终态事件：本分支是实际检测到 run 取消的执行点，须发出
        # RUN_CANCELLED 供前端 StatusBadge 渲染；工具尚未执行无 token 累积，
        # 经统一 emit_run_cancelled 构造（携带 langfuse_trace_id，与 model/observe 一致）。
        # 取消前已发射的 pending tool_call 事件（含参数非法的 pending）由 lifecycle.cancel
        # 全部收口为 cancelled，避免任何悬空 part。
        all_calls = [
            ToolCall.from_dict(
                {
                    "tool_name": record.tool_name,
                    "call_id": record.tool_call_id,
                    "arguments": record.args,
                }
            )
            for record in (lifecycle.calls.values() if lifecycle else [])
        ]
        if lifecycle is None:
            raise RuntimeError("tool_call_lifecycle is required before tools_node cancellation")
        lifecycle = lifecycle.cancel(
            task_id=task_id,
            run_id=run_id,
            step_id=step_id,
            tool_calls=all_calls,
        )
        operations.cancel_run_if_running(end_reason="runtime_cancelled", usage_stats=rc.usage_stats)
        return {
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "last_tool_results": {},
            "tool_call_lifecycle": lifecycle,
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
    tool_task = asyncio.create_task(
        asyncio.to_thread(
            operations.run_tool_calls,
            str(task.id),
            approved_calls,
            step_id,
            running_loop=asyncio.get_running_loop(),
        )
    )
    try:
        tool_run: ToolRunResult = await asyncio.shield(tool_task)
    except asyncio.CancelledError:
        # 取消外层 graph task 不会自动停止 to_thread 的 worker。先等待 worker 收束，
        # 再让 executor 释放 task lock，避免用户立即 resume 时与旧工具写入重叠。
        try:
            await tool_task
        except BaseException as error:
            log.warning(
                "tools_node_cancelled_worker_failed",
                extra={
                    "msg": "取消时等待工具 worker 收束失败",
                    "data": {
                        "run_id": run_id,
                        "step_id": step_id,
                        "error_type": type(error).__name__,
                    },
                },
            )
        raise
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
    log.info(
        "tools_node_completed",
        extra={
            "msg": f"工具执行完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(observations),
            },
        },
    )

    return {
        "last_tool_results":
            {
                "instruction": instruction or "",
                "observations": [dataclasses.asdict(observation) for observation in observations],
                "expected_call_ids": [call.call_id for call in approved_calls],
            },
    }
