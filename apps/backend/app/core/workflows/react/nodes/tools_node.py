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

执行集合取 ``valid_calls`` + ``blocked_calls`` 中**尚未起跑**的记录（``status == "pending"``）；
``begin`` 迁移出的 ``running`` 快照随 patch 写回 state，使 checkpoint 反映本批实际起跑状态。

**剩余缺口（未决）**：LangGraph 的 checkpoint 以节点为单位——本节点执行期间（工具正在跑）进程
退出时，checkpoint 仍停留在进入本节点之前（记录仍是 ``pending``），因此续跑重入本节点仍会重放
本批调用，对 ``write_file`` / ``apply_patch`` / ``delete_file`` / ``execute_terminal`` 这类有副作用
工具会造成二次执行。彻底消除必须在「工具起跑前」落盘一份「已起跑」事实（按 run_id + step_id +
tool_call_id 的幂等键）作为过滤依据，该事实源尚未建立。模型协议的配对闭合由
``model_node.load_message`` 兜底。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses
from typing import Any

from app.config.logging.logger import log
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.workflows.react.node_helper.common import _runtime_config
from app.core.workflows.react.worflow_state.state import ReactGraphState

_TERMINAL_CHECKPOINT_FIELDS = frozenset(
    {
        "session_id",
        "status",
        "initial_cwd",
        "shell_kind",
        "first_available_seq",
        "next_seq",
        "exit_code",
        "end_reason",
    }
)


def _project_terminal_sessions(
    previous: dict[str, dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """把终端工具展示元数据合并到当前 Run 的 checkpoint state。

    只接受 ``display_data.kind == terminal-session`` 的 allowlisted 字段；工具结果中的
    原始 output、诊断文本和进程对象不会进入 checkpoint。活终端清理由 runtime registry
    负责，不依赖这个投影执行。
    """

    projected = {session_id: dict(value) for session_id, value in previous.items()}
    for observation in observations:
        display_data = observation.get("display_data")
        if not isinstance(display_data, dict) or display_data.get("kind") != "terminal-session":
            continue
        session_id = display_data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        current = projected.setdefault(session_id, {"session_id": session_id})
        for field in _TERMINAL_CHECKPOINT_FIELDS:
            value = display_data.get(field)
            if value is not None:
                current[field] = value
    return projected


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
        需要合并回 graph state 的增量：``last_tool_results``（本批次工具观察的
        ``dataclasses.asdict`` 投影，键名即执行层字段名 ``tool_call_id`` / ``display_data``，
        可落 checkpoint）、``terminal_sessions``（终端会话展示元数据投影）与
        ``tool_call_lifecycle``（``begin`` 迁移后的快照：已起跑的合法调用为 ``running``，
        隐藏闭合调用仍为 ``pending``）。写回该快照让 checkpoint 反映本批实际起跑状态，
        重入时无需对同一批调用重复发 ``running`` 事件。

        执行集合在 ``begin`` 之前按 ``pending`` 判定：``running`` / 终态记录已起跑或已有观察，
        再次发起会对有副作用的工具造成二次写入。

    异常:
        无。工具链路异常由执行层收口为 ``ToolObservation``（含 ``error`` 观察）。

    副作用:
        - 执行工具（文件、终端、搜索、委派等）并产出工具生命周期事实；状态写入 **run**；
        - ``running`` 状态事件由 ``begin`` 在本节点发出；终态事件不在本节点发出，也不在此
          收口取消（两者分别由执行层出口投影与 ``observe`` 节点的 ``settle`` 负责）；
        - 模型协议层面的配对闭合统一由 ``RuntimeContextManager.load_message`` 在下次取数时
          自动补 ``ToolMessage`` 占位，本节点不构造/落库占位消息、亦不越界访问 service
          受保护成员。
    """
    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    lifecycle = state.tool_call_lifecycle
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）
    if lifecycle is None:
        raise RuntimeError("tool_call_lifecycle is required before tools_node execution")

    instruction = state.instruction

    # run 身份取自 ``RuntimeConfig.run``（``ReactGraphState`` 不含 run_id，状态事实源是 run 记录）。
    # 可执行集合必须在 ``begin`` **之前**判定：``begin`` 会把待执行的合法调用迁移为 ``running``，
    # 若在其后过滤就只剩隐藏闭合记录。取合法调用 + 隐藏闭合集合中尚未起跑的记录（``pending``）：
    # 后者由 ToolAccessGate 在执行层拒绝，其错误观察用于闭合模型协议（前端无对应 part，由
    # settle 只写 ToolMessage、不发终态事件）。已 running / 终态的记录不再发起：它们已经起跑或
    # 已有观察，重复发起会对 write_file / apply_patch / delete_file / execute_terminal 这类有
    # 副作用的工具造成二次写入。
    pending_records = [
        record
        for record in lifecycle.valid_tools + lifecycle.blocked_tool_calls
        if record.status == "pending"
    ]
    lifecycle = lifecycle.begin(task_id=task_id, run_id=rc.run.id, step_id=step_id)
    approved_calls = [_to_tool_call(record) for record in pending_records]

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

    tool_run = await operations.run_tool_calls(
        task_id, approved_calls, step_id, asyncio.get_running_loop()
    )

    log.info(
        "tools_node_tool_run",
        extra={
            "msg": f"工具批次执行结果，step_id={step_id}",
            "data": {"tool_run": dataclasses.asdict(tool_run)},
        },
    )

    observations:list[ToolObservation] = tool_run.observations  # 每个工具调用的观察结果
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

    observation_dicts = [dataclasses.asdict(observation) for observation in observations]

    return {
        "last_tool_results": {
            "instruction": instruction or "",
            "observations": observation_dicts,
        },
        "terminal_sessions": _project_terminal_sessions(
            state.terminal_sessions,
            observation_dicts,
        ),
        "tool_call_lifecycle": lifecycle,
    }
