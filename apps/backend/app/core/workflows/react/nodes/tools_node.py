"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：执行本批工具调用并产出可序列化观察摘要。
工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实由
``ToolCallLifecycleManager`` 写入 canonical conversation state。本节点**不做**终态事件分发
（``completed`` / ``failed`` / ``cancelled``）、不写回模型上下文、不做错误计数与上限判定，
也不做协作取消收口——这些全部收敛到 ``observe`` 节点做「工具结果观察处理」的单一收口
（见 ``observation_node`` 与 ``tool_call_lifecycle`` 的 ``ToolCallLifecycleManager``）。

本节点产出可序列化摘要（``last_tool_results``，即本批 ``ToolObservation`` 的
``dataclasses.asdict`` 投影，键名与执行层字段一致，含 ``tool_call_id`` /
``display_data``）供 ``observe`` 消费。

注意：本节点**不会重放**旧 checkpoint 里的工具批次，该性质与 ``checkpoint_thread_id``
无关（续跑复用原线程、从既有 checkpoint 继续）。真正生效的是「只执行活调用」：
本节点只从 ``ToolCallLifecycleManager`` 取 ``status == "running"`` 的调用，因此从既有
checkpoint 恢复时待执行集合为空、直接短路返回空摘要。模型协议的配对闭合由
``model_node.load_message`` 兜底。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses

from app.config.logging.logger import log
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall
from app.core.workflows.react.nodes.helper.common import _runtime_config

from app.core.workflows.react.state import ReactGraphState


def _to_tool_call(record: object) -> ToolCall:
    """把一条生命周期记录还原为 ``ToolCall``（本模块唯一的记录→调用映射点）。

    参数:
        record: ``ToolCallLifecycleRecord`` 实例。

    返回:
        等价的 ``ToolCall``；记录字段缺失时以空值构造（由 ``ToolCall`` 自身校验）。

    异常:
        无。

    副作用:
        无。
    """

    return ToolCall.from_dict(
        {
            "tool_name": getattr(record, "tool_name", ""),
            "call_id": getattr(record, "tool_call_id", ""),
            "arguments": getattr(record, "args", {}),
        }
    )


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：执行本批工具调用并产出结果摘要供 observe 观察。

    工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实经明确的开始/完成回调写入
    canonical state。本节点只负责「取 running 调用 + 执行 + 产出摘要」；
    终态事件（``completed`` / ``failed`` / ``cancelled``）、模型上下文写回、错误计数
    与上限判定全部下沉到 ``observe`` 节点。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求。生命周期回调在协程内
    构造后闭包捕获，并随工具执行传入工作线程，不依赖 LangGraph stream writer。

    参数:
        state: 当前 graph state，经 ``tool_call_lifecycle`` 携带待执行工具调用与 instruction。

    返回:
        需要合并回 graph state 的增量：
        - 无 ``running`` 调用（例如仅参数非法的 ``pending`` 调用）时短路返回：回写
          ``tool_call_lifecycle``，并把 ``last_tool_results`` 重置为结构完整的空摘要
          （``{"instruction": ..., "observations": [], "expected_call_ids": []}``），使
          ``observe`` 不重复结算上一批结果、也不因缺键崩溃；
        - 正常分支只回写 ``last_tool_results``（本批次工具观察的 ``dataclasses.asdict``
          投影，键名即执行层字段名 ``tool_call_id`` / ``display_data``，可落 checkpoint），
          ``tool_call_lifecycle`` 沿用 ``model`` 节点已写入的快照。

    异常:
        无。工具链路异常由执行层收口为 ``ToolObservation``（含 ``error`` 观察）。

    副作用:
        - 执行工具（文件、终端、搜索、委派等）并产出工具生命周期事实；状态写入 **run**；
        - ``running`` 与终态事件不在本节点发出，也不在此收口取消；
        - 模型协议层面的配对闭合统一由 ``RuntimeContextManager.load_message`` 在下次取数时
          自动补 ``ToolMessage`` 占位，本节点不构造/落库占位消息、亦不越界访问 service
          受保护成员。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    lifecycle = state.tool_call_lifecycle
    if lifecycle is None:
        raise RuntimeError("tool_call_lifecycle is required before tools_node execution")
    # 仅执行状态为 running 的合法调用；pending（参数非法）调用不执行，由 observe 节点统一结算。
    approved_calls = [
        _to_tool_call(record)
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

    tool_run: ToolRunResult = await operations.run_tool_calls(
        task_id, approved_calls, step_id, asyncio.get_running_loop()
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
    # observe 节点（经 ToolCallLifecycleManager.settle_batch 分发），本节点只产出治理摘要。
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
