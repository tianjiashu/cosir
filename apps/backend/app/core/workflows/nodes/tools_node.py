"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：工具批次执行与执行前取消检查。
工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事实由
``ToolCallLifecycleManager`` 写入 canonical conversation state。本节点**不再**分发终态事件
（``completed`` /
``failed`` / ``cancelled``）、不再写回模型上下文、不做错误计数/上限判定——
全部收敛到 ``observe`` 节点做「工具结果观察处理」的单一收口（见 ``observation_node`` 与
``tool_call_lifecycle`` 的 ``ToolCallLifecycleManager``）。

本节点产出可序列化摘要（``last_tool_results``，即本批 ``ToolObservation`` 的
``dataclasses.asdict`` 投影，键名与执行层字段一致，含 ``tool_call_id`` /
``display_data``）供 ``observe`` 消费。执行前检测到协作取消时，本节点**不落终态、不结束
图**，只置 ``cancel_requested`` 并回写 lifecycle；取消的统一收口（工具调用统一取消 +
取消终态落库）由 ``observe`` 节点承担，图随后停在 ``pause``。

注意：本节点**不会重放**旧 checkpoint 里的工具批次，该性质与 ``checkpoint_thread_id``
无关（续跑复用原线程、从既有 checkpoint 继续）。真正生效的是两层「只执行活调用」：
本节点只从 ``ToolCallLifecycleManager`` 取 ``status == "running"`` 的调用，而取消路径会把
本批调用统一收口为 ``cancelled``（见 ``observation_node`` 的协作取消收口），因此从既有
checkpoint 恢复时待执行集合为空、直接短路返回空摘要。模型协议的配对闭合由
``model_node.load_message`` 兜底。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses

from app.config.logging.logger import log
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall
from app.core.workflows.nodes.helper.common import _runtime_config

from ..react.state import ReactGraphState


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
        直接短路返回空摘要（不经 ``run_tool_calls``），由 ``observe`` 把 Agent 推回下一轮
        推理；执行前检测到协作取消时置 ``cancel_requested`` 并返回结构完整的空摘要
        （``last_tool_results`` 必须清空，否则 ``observe`` 会重复结算上一批结果），取消的
        统一收口与图的路由分别由 ``observe`` 与 ``_after_observe`` 决定。

    副作用:
        - 工具生命周期事实经明确回调写入 canonical state；状态写入 **run**；
        - ``running`` 与终态事件不在本节点发出；执行前检测到协作取消时提前 return，
          不进入 ``run_tool_calls``、不派生 worker，工具调用的统一收口由 ``observe``
          节点执行（见 ``observation_node``）；
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
