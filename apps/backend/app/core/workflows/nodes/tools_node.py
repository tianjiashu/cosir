"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：在权限审批后执行工具并把观察结果追加回运行时上下文。
审批编排（按 ``RuntimeConfig.approval_resolver`` 决定是否 ``interrupt()`` 暂停等待人工
审批）已抽离到独立模块 ``approval``（``resolve_approved_calls``），本节点负责审批恢复后
的取消检查、工具执行与占位闭合配对。工具执行通过 ``RuntimeOperations`` 完成，工具生命
周期事件经 ``write_event`` 回调写入自定义事件流；观察消息由 bridge 转为 ``BaseMessage``
经 ``_persist_tool_observations`` 写回 ``RuntimeContextManager``（**不进 graph state**，
模型上下文由 RuntimeContextManager 独占）。状态写入 **turn**。

工具观察的增量落库与写回统一收敛在 ``_persist_tool_observations``：落库一条即写回一条，
避免「部分落库、零写回」的撕裂状态，闭合上一轮模型节点写入的 ``AIMessage.tool_calls``
配对。执行后取消判断与 ``deferred_repair_message`` 注入已下沉到 ``observe`` 节点
（见 ``observation_node``），本节点只负责「审批 + 执行 + 配对闭合」，正常返回后经条件边
进 ``observe`` 做「工具结果观察处理」的单一收口。执行前取消分支（工具尚未执行、无结果可
观察）仍保留在本节点，补占位落库后直接终态。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import dataclasses
from typing import Any

import sqlalchemy
from langgraph.types import interrupt

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.workflows.nodes.helper.approval import resolve_approved_calls
from app.core.workflows.nodes.helper.common import (
    _make_write_event,
    _runtime_config,
    _runtime_context,
    emit_run_cancelled,
)
from app.models import RuntimeMessage
from app.service.tool_execution.run_result import ToolRunResult
from app.tools.schemas import ToolCall, ToolObservation
from app.utils.trace_infra.redaction import redact_terminal_output

from ..react.state import ReactGraphState


def _persist_tool_observations(
        obs_messages: list[RuntimeMessage],
) -> None:
    """将一批工具观察消息经 ``RuntimeContextManager`` 落库并同步写回运行时上下文。

    落库与写回逐条配对（落库一条即写回一条），避免「部分落库、零写回」的
    撕裂状态；写回使下一轮模型节点经 ``_runtime_context().load_message()`` 能
    看到本轮工具结果，闭合上一轮 ``_model_node`` 写入的 ``AIMessage.tool_calls``
    配对，防止悬空 ``tool_calls`` 触发 OpenAI 协议校验失败。消息**不进 graph state**，
    模型上下文由 ``RuntimeContextManager`` 独占管理。

    参数:
        obs_messages: 本批次工具观察 ``RuntimeMessage`` 列表（已含 ``tool_call_id`` 元数据）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 落库失败时由 ``add_message`` 透传，
        此时后续消息不写回（保持 DB 与上下文一致：DB 失败则上下文也不写）。
        IndexError / ValueError / KeyError: ``add_message`` 转换/写回路径的
        数据形态异常（防御性捕获），与 ``SQLAlchemyError`` 同路径处理。
        上述异常均提取 ``tool_call_id`` 与批次进度写入 error 日志后原样重抛。

    副作用:
        逐条调用 ``_runtime_context().add_message(obs_message)``：先落库（失败抛
        异常、内存不写），成功后再经 manager 转 ``BaseMessage`` 写回运行时上下文。
    """
    total = len(obs_messages)
    for index, obs_message in enumerate(obs_messages):
        try:
            log.info(
                "add_tool_observation",
                extra={
                    "msg": "添加工具观察",
                    "data": {"message": dataclasses.asdict(obs_message)},
                },
            )
            _runtime_context().add_message(obs_message)
        except (sqlalchemy.exc.SQLAlchemyError, IndexError, ValueError, KeyError) as exc:
            # 仅捕获可预期的落库/上下文异常；其他异常（如编程错误）直接抛出不被吞。
            tool_call_id = (obs_message.metadata or {}).get("tool_call_id", "")
            log.exception(
                "persist_tool_observations_failed",
                extra={
                    "msg": (
                        f"工具观察增量落库失败，已处理 {index}/{total} 条，"
                        f"tool_call_id={tool_call_id}"
                    ),
                    "data": {
                        "processed": index,
                        "total": total,
                        "tool_call_id": tool_call_id,
                        "error": str(exc),
                    },
                },
            )
            raise


def _build_tool_result_summaries(
        observations: list[ToolObservation],
        instructions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把一批工具观察结果压缩为可序列化摘要，供 ``observe`` 节点判定与后续 LLM 观察使用。

    摘要只保留原生类型（dict / str / bool），可直接落入 checkpoint；刻意**不承载**
    ``data`` 等大体积结构化字段，``content`` 经 ``redact_terminal_output`` 脱敏后截断到
    ``Settings.TOOL_OBSERVATION_CONTEXT_LIMIT``，既避免明文凭据落盘 checkpoint，也避免撑爆
    checkpoint，同时保留足够文本供后续「LLM 观察工具结果」推理。脱敏复用 ``utils.trace_infra``
    既有实现，不自写凭据掩码逻辑。

    参数:
        observations: 本批次工具观察 ``ToolObservation`` 列表（成功/失败均含）。
        instructions: 可选，``call_id`` → 模型调工具前说明文本的映射（来自 ``model`` 节点
            写入 ``pending_tool_calls`` 的 ``instruction`` 键）。提供时一并写入摘要，使
            ``observe`` 节点在错误上限等分支能看到模型当时的意图。缺省视为空映射。

    返回:
        dict 列表，每项字段固定为 ``call_id`` / ``tool_name`` / ``status`` / ``error`` /
        ``reason`` / ``content``（脱敏后截断）/ ``retryable`` / ``instruction``（模型意图，
        无则空串）；顺序与 ``observations`` 一致。
    """
    instructions = instructions or {}
    limit = Settings.TOOL_OBSERVATION_CONTEXT_LIMIT
    summaries: list[dict[str, Any]] = []
    for observation in observations:
        content = redact_terminal_output(observation.content)
        if len(content) > limit:
            content = content[:limit]
        summaries.append(
            {
                "call_id": observation.tool_call_id,
                "tool_name": observation.tool_name,
                "status": observation.status,
                "error": observation.error,
                "reason": observation.reason,
                "content": content,
                "retryable": observation.retryable,
                "instruction": instructions.get(observation.tool_call_id, ""),
            }
        )
    return summaries


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：审批 + 执行 + 配对闭合，产出工具结果摘要供 observe 观察。

    节点按 ``RuntimeConfig.approval_resolver`` 决定是否需要审批（审批编排抽离到
    ``approval.resolve_approved_calls``，行为契约见该模块）：

    - 存在 ``approval_resolver``：用 ``interrupt()`` 暂停 graph 等待审批，审批结果
      （批准的工具调用列表）通过 ``Command(resume=)`` 恢复；随后执行工具。
    - 不存在 ``approval_resolver``（``None``）：视为「自动放行全部调用」，
      **不经过 ``interrupt()``**，直接以 ``state.pending_tool_calls`` 作为已批准列表执行工具。
      这样 graph 不会暂停，编排层循环可正常走到终态，避免「无审批器时反复
      interrupt→resume 同一工具调用」的死循环。

    工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事件经 ``write_event`` 回调写入
    自定义事件流；观察消息由 bridge 转为 ``BaseMessage`` 经 ``_persist_tool_observations``
    写回 ``RuntimeContextManager``（**不进 graph state**，模型上下文由
    RuntimeContextManager 独占）。
    状态写入 **turn**。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求，运行期增量
    也就无从实时到达客户端。工作线程内的事件写入使用**闭包捕获的 writer**
    （见 ``_make_write_event``），因为 ``get_stream_writer()`` 依赖 LangGraph 的
    运行上下文，需在协程内先取出再带入线程。

    参数:
        state: 当前 graph state，含待执行工具调用。

    返回:
        需要合并回 graph state 的增量：正常分支 ``last_tool_results`` 为本批次工具结果摘要
        （可序列化 dict 列表，供 ``observe`` 节点观察判定与后续 LLM 观察），并清空
        ``pending_tool_calls``；执行前取消分支置 ``terminal=True`` 且返回
        ``last_tool_results=[]``——因为 ``_after_tools`` 在 ``terminal`` 时直接 END、不进
        observe，返回摘要既无人消费又会撑大 checkpoint。执行后取消判断、错误计数/上限判定
        与 ``deferred_repair_message`` 注入均已下沉到 ``observe`` 节点，本节点不再处理。

    副作用:
        - 经 ``_persist_tool_observations`` 把本批次工具观察消息逐条增量落库，并同步
          ``_runtime_context().add_message`` 写回运行时上下文，闭合上一轮 ``_model_node``
          写入的 ``AIMessage.tool_calls`` 配对；
        - 执行前取消分支（工具尚未执行、无结果可观察，不能下沉 observe）置 ``terminal=True``
          让 graph 走 END；因提前 return 不进入 ``run_tool_calls``，由本节点经
          ``RuntimeOperations.build_cancel_placeholder_messages`` 公开门面为上一轮
          ``tool_calls`` 补同构占位并写回（占位字段与序列化逻辑 100% 同源 service，
          不平行复制），消除 core 对 service 受保护成员的越界访问；
        - 工具生命周期事件经 ``write_event`` 透传；状态写入 **turn**。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    turn = operations.get_current_turn()  # 当前 turn 记录

    tool_calls = state.pending_tool_calls  # 来自 model 节点写入的待执行工具调用
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）

    # 在 LangGraph 运行上下文内取出 writer 并闭包捕获：取消分支与工作线程均复用，
    # 避免取消分支晚于 writer 定义而取不到运行上下文。
    node_write_event = _make_write_event()

    # 审批编排（独立模块）：无审批器自动放行，有审批器 interrupt 暂停 + resume。
    # 注：interrupt 是「挂起续跑」而非「失败重放」——resume 后从此调用点原地继续，
    # 本函数体不会从头重跑，故下方 run_tool_calls / _persist_tool_observations 仅执行一次，
    # 不会因 checkpoint 重放而重复落库（model_node 也无 interrupt，不会被 resume 触发重跑）。
    approved_dicts = resolve_approved_calls(rc, tool_calls, step_id, interrupt)

    # ★ 取消检查：审批恢复后（或自动放行时）、工具执行前，若 turn 已被取消则跳过工具执行。
    # 第零铁律（正确性优先）：本分支提前 return，不进入下方 ``run_tool_calls`` 路径，故
    # ``ToolExecutionService`` 的取消兜底（``_build_result_with_cancel_placeholders``）在此
    # 不会执行。但上一轮 ``_model_node`` 已把 ``AIMessage.tool_calls``
    # 写入 ``RuntimeContextManager``，必须在本分支内为它们补同构占位 ``ToolMessage``，
    # 否则下一轮模型请求会因悬空 ``tool_calls``
    # 触发 OpenAI 协议校验失败。为保持与 service 内部协议字段完全同构、避免平行复制语义漂移，
    # 经 ``RuntimeOperations.build_cancel_placeholder_messages`` 公开门面复用 service 的取消占位
    # 实现（而非自拼 JSON、不手调 ``tool_cancelled`` 工厂），仅作「配对闭合」这一件职责，
    # 执行/事件/广播仍由 service 承担。
    if operations.is_current_turn_cancelled():
        log.info(
            "tools_node_cancelled",
            extra={
                "msg": f"工具节点恢复后检测到 turn 已取消，跳过工具执行，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.id},
            },
        )
        # 配对闭合：为上一轮已写出的 tool_calls 补 cancelled 占位。执行前分支提前
        # return 不进 run_tool_calls，service 的兜底（_build_result_with_cancel_placeholders）
        # 对此路径不生效，故调用 service 公开能力构造同构占位，保证协议字段与正常
        # 执行路径（含执行中取消）100% 同源，不平行复制序列化逻辑。
        cancel_calls = [
            ToolCall.from_dict(call)
            for call in state.pending_tool_calls
            # 不过滤call_id未定义的情况，统一补占位，避免不配对
        ]
        cancel_placeholders = operations.build_cancel_placeholder_messages(cancel_calls)
        _persist_tool_observations(cancel_placeholders)
        # 收口取消终态事件：本分支是实际检测到 turn 取消的执行点，须发出
        # RUN_CANCELLED 供前端 StatusBadge 渲染；工具尚未执行无 token 累积，
        # 经统一 emit_run_cancelled 构造（携带 langfuse_trace_id，与 model/observe 一致）。
        emit_run_cancelled(rc, step_id)
        return {
            "pending_tool_calls": [],
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "last_tool_results": [],
            # 清空 deferred 防 checkpoint 残留（与 observe 各路径防残留契约一致）；
            # terminal=True 直接 END 不消费，但避免下一轮/恢复时读到脏值。
            "deferred_repair_message": "",
        }

    # 把审批结果 dict 重建为内部 ToolCall 值对象（经 ToolCall.from_dict 统一兜底字段，
    # 与取消分支同源，避免字段增减时两处分支漂移）。
    approved_calls = [ToolCall.from_dict(item) for item in approved_dicts]

    # 真正执行工具（内部会发工具生命周期事件，write_event 作为回调注入）。
    # 若模型本轮调工具前附带说明文本（instruction），一并带出供排查时看到模型意图。
    # 经 redact_terminal_output 脱敏，避免说明文本意外含凭据等敏感信息落日志。
    raw_instruction = (approved_dicts[0].get("instruction", "") if approved_dicts else "")
    instruction = redact_terminal_output(raw_instruction)
    log.info(
        "tools_node_resumed",
        extra={
            "msg": f"审批已恢复，准备执行 {len(approved_calls)} 个工具调用，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "approved_count": len(approved_calls),
                "instruction": instruction,
            },
        },
    )
    # node_write_event 已在函数顶部（LangGraph 运行上下文内）取出并闭包捕获：
    # 工具批次在工作线程执行，线程内无法再依赖 get_stream_writer() 的运行上下文。
    # 事件循环也需在此取出并传入，供命令运行期输出增量从工作线程调度回环广播。
    tool_run: ToolRunResult = await asyncio.to_thread(
        operations.run_tool_calls,
        task.id,
        approved_calls,
        step_id,
        write_event=node_write_event,
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
    # 逐条持久化本轮产生的工具观察消息，并同步写回运行时上下文
    # （替代 turn 结束后的批落库；写回使下一轮模型节点能看到工具结果）。
    # 落库与写回逐条配对：落库一条即写回一条，避免「部分落库、零写回」撕裂。
    _persist_tool_observations(tool_run.messages_for_model)

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

    # 把本批工具调用的 instruction（模型调工具前说明）按 call_id 收成映射，写入结果摘要，
    # 供 observe 节点在错误排查时看到模型意图。
    instructions = {
        item.get("call_id", ""): item.get("instruction", "")
        for item in approved_dicts
        if item.get("call_id")
    }
    # 本节点只负责「执行 + 落库写回 + 产出结果摘要」；连续失败计数与错误上限判定
    # 下沉到 observe 节点，由它读取 last_tool_results 后决定是否发 RUN_FAILED 并终态。
    return {
        "pending_tool_calls": [],  # 清空待执行工具调用
        "last_tool_results": _build_tool_result_summaries(observations, instructions),
    }
