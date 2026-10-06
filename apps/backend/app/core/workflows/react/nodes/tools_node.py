"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：分类模型交接的原始调用、治理工具调用生命周期、执行获准
调用并产出可序列化观察摘要。工具执行通过 ``RuntimeOperations`` 完成；工具结果终态、模型
上下文写回与执行错误计数由 ``observe`` 节点收口。

本节点产出可序列化摘要（``last_tool_results``，即本批 ``ToolObservation`` 的
``dataclasses.asdict`` 投影，键名与执行层字段一致，含 ``tool_call_id`` /
``display_data``）供 ``observe`` 消费。

只有真实执行的结果摘要会进入 ``observe``；纯无效或纯阻塞批次在本节点完成协议闭合与提示注入，
然后直接回到 ``model``。混合批次的拒绝反馈延迟到 ``observe`` 写入全部 ``ToolMessage`` 之后，
避免破坏模型消息顺序。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses
from typing import Any

from langchain_core.messages import SystemMessage

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.runtime.run_result import ToolRunResult
from app.core.tools.schemas import ToolCall, ToolObservation
from app.core.workflows.react.node_helper.common import _runtime_config, _runtime_context
from app.core.workflows.react.node_helper.tool_call_lifecycle import (
    ToolCallLifecycleManager,
)
from app.core.workflows.react.worflow_state.route import ReactRoute
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
    """分类并处理模型工具请求，只把真实执行结果交给 observe。

    本节点是工具策略、生命周期与执行的唯一入口：先区分获准、无效和阻塞调用，创建调用事件，
    收口拒绝调用，再仅执行获准集合。纯拒绝批次直接闭合协议、写入受控提示并回到 model；混合
    批次把拒绝反馈延迟到 observe，使其位于全部工具结果消息之后。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求。生命周期回调在协程内
    构造后闭包捕获，并随工具执行传入工作线程，不依赖 LangGraph stream writer。

    参数:
        state: 当前 graph state，``tool_request`` 携带 model 节点交接的原始调用。

    返回:
        需要合并回 graph state 的增量：
        - 纯拒绝批次直接回 model，不进入 observe；
        - 含获准调用时回写 ``last_tool_results``、``tool_call_lifecycle`` 并路由 observe。

    异常:
        无。工具链路异常由执行层收口为 ``ToolObservation``（含 ``error`` 观察）。

    副作用:
        - 执行工具（文件、终端、搜索、委派等）并产出工具生命周期事实；状态写入 **run**；
        - 发出工具创建、运行及无效调用终态事件，执行工具，并为阻塞调用写入协议闭合消息；
        - 纯拒绝批次把受控提示写入 ``RuntimeContextManager``，不会调用空工具批次。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    task_id = task.id
    instruction = state.instruction
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）

    request = state.tool_request
    raw_tool_calls = request.get("tool_calls", [])
    raw_invalid_calls = request.get("invalid_tool_calls", [])
    known_tool_names = {tool.name for tool in operations.all_vaild_tools}
    tool_calls: list[ToolCall] = []
    invalid_tool_calls = [dict(call) for call in raw_invalid_calls if isinstance(call, dict)]
    creatable_calls: list[dict[str, object]] = []
    protocol_call_ids: set[str] = set()

    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, dict):
            invalid_tool_calls.append({"error": "Malformed tool call"})
            continue
        tool_name = raw_call.get("name")
        call_id = raw_call.get("id")
        arguments = raw_call.get("args")
        if isinstance(call_id, str) and call_id:
            protocol_call_ids.add(call_id)
        if isinstance(tool_name, str) and tool_name in known_tool_names and call_id:
            creatable_calls.append({"id": str(call_id), "name": tool_name})
        if (
            isinstance(tool_name, str)
            and tool_name in known_tool_names
            and isinstance(call_id, str)
            and call_id
            and isinstance(arguments, dict)
        ):
            tool_calls.append(
                ToolCall.from_from_langchain({"name": tool_name, "id": call_id, "args": arguments})
            )
            continue
        invalid_tool_calls.append(
            {
                **raw_call,
                "error": raw_call.get("error") or "Tool call is malformed or unknown.",
            }
        )

    for invalid_call in invalid_tool_calls:
        name = invalid_call.get("name")
        call_id = invalid_call.get("id")
        if (
            isinstance(name, str)
            and name in known_tool_names
            and isinstance(call_id, str)
            and call_id
        ):
            creatable_calls.append({"id": call_id, "name": name})

    lifecycle = ToolCallLifecycleManager(allows_tools=tuple(operations.allows_tools))
    lifecycle = lifecycle.create(
        task_id=task_id,
        run_id=operations.get_current_run().id,
        step_id=step_id,
        raw_tool_calls=creatable_calls,
    )
    lifecycle = lifecycle.classify(
        task_id=task_id,
        run_id=operations.get_current_run().id,
        step_id=step_id,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
    )
    lifecycle.close_blocked_calls(
        task_id=task_id,
        run_id=operations.get_current_run().id,
        step_id=step_id,
        tool_call_ids=protocol_call_ids,
    )
    repair_inputs = [
        call
        for call in invalid_tool_calls
        if not call.get("id") or str(call.get("id")) not in lifecycle.blocked_calls
    ]
    lifecycle, repair_message = lifecycle.fail_invalid_tools(
        task_id=task_id,
        run_id=operations.get_current_run().id,
        step_id=step_id,
        invalid_tool_calls=repair_inputs,
    )
    approved_calls = [_to_tool_call(record) for record in lifecycle.valid_tools]
    blocked_names = sorted({record.tool_name for record in lifecycle.blocked_calls.values()})
    rejected = bool(blocked_names or repair_inputs)
    rejection_count = state.tool_rejection_count + 1 if rejected else 0
    final_answer_only = state.final_answer_only or (
        rejected and rejection_count > Constant.Workflow.TOOL_REJECTION_RETRY_LIMIT
    )
    feedback_parts: list[str] = []
    if repair_message:
        feedback_parts.append(repair_message)
    if blocked_names:
        feedback_parts.append(
            "The following tools are disabled for this run and were not executed: "
            f"{', '.join(blocked_names)}. Do not call them again. Continue with available results "
            "or other allowed tools."
        )
    feedback = "\n\n".join(feedback_parts)

    log.info(
        "tools_node_classified",
        extra={
            "msg": (
                f"工具请求分类完成，准备执行 {len(approved_calls)} 个获准调用，"
                f"step_id={step_id}"
            ),
            "data": {
                "step_id": step_id,
                "approved_count": len(approved_calls),
                "blocked_count": len(lifecycle.blocked_calls),
                "invalid_count": len(repair_inputs),
                "instruction_length": len(instruction),
            },
        },
    )

    if not approved_calls:
        if feedback:
            _runtime_context().add_message(
                SystemMessage(
                    content=feedback,
                    additional_kwargs={"run_id": operations.get_current_run().id},
                )
            )
        return {
            "next_node": ReactRoute.MODEL,
            "tool_call_lifecycle": lifecycle,
            "tool_request": {},
            "tool_rejection_count": rejection_count,
            "final_answer_only": final_answer_only,
            "tool_feedback": "",
            "last_tool_results": {
                "instruction": instruction,
                "observations": [],
                "expected_call_ids": [],
            },
        }

    tool_run: ToolRunResult = await operations.run_tool_calls(
        task_id, approved_calls, step_id, asyncio.get_running_loop()
    )

    log.info(
        "tools_node_tool_run",
        extra={
            "msg": f"工具批次执行结果，step_id={step_id}",
            "data": {
                "observation_count": len(tool_run.observations),
                "status_counts": {
                    status: sum(1 for item in tool_run.observations if item.status == status)
                    for status in sorted({item.status for item in tool_run.observations})
                },
            },
        },
    )

    observations: list[ToolObservation] = tool_run.observations  # 每个工具调用的观察结果
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
        "next_node": ReactRoute.OBSERVE,
        "tool_request": {},
        "tool_rejection_count": rejection_count,
        "final_answer_only": final_answer_only,
        "tool_feedback": feedback,
        "tool_call_lifecycle": lifecycle,
        "last_tool_results": {
            "instruction": instruction or "",
            "observations": observation_dicts,
            "expected_call_ids": [call.call_id for call in approved_calls],
        },
        "terminal_sessions": _project_terminal_sessions(
            state.terminal_sessions,
            observation_dicts,
        ),
    }
