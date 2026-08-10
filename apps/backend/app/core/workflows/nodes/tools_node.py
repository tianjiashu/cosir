"""ReAct-like 工作流的工具节点（``_tools_node``）。

本模块只承载「工具节点」单一职责：在权限审批后执行工具并把观察结果追加回上下文。节点按
``RuntimeConfig.approval_resolver`` 决定是否需要审批；工具执行通过 ``RuntimeOperations``
完成，工具生命周期事件经 ``write_event`` 回调写入自定义事件流；观察消息由 bridge 转为
``BaseMessage`` 存回 state。状态写入 **turn**。

工具观察的增量落库与写回统一收敛在 ``_persist_tool_observations``：落库一条即写回一条，
避免「部分落库、零写回」的撕裂状态，闭合上一轮模型节点写入的 ``AIMessage.tool_calls``
配对。与模型节点共享的运行时原语见 ``common``。
"""

import asyncio
import json

import sqlalchemy
from langgraph.types import interrupt

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.llm.langchain_bridge import runtime_to_langchain
from app.core.runtime.runtime_operations import RuntimeOperations
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.payload import RunFailedPayload
from app.tools.schemas import ToolCall

from ..react.state import ReactGraphState
from .common import _make_write_event, _runtime_config, _runtime_context, write_event


def _persist_tool_observations(
    operations: RuntimeOperations,
    obs_messages: list[RuntimeMessage],
) -> None:
    """将一批工具观察消息落库并同步写回运行时上下文。

    落库与写回逐条配对（落库一条即写回一条），避免「部分落库、零写回」的
    撕裂状态；写回使下一轮模型节点经 ``_runtime_context().load_message()`` 能
    看到本轮工具结果，闭合上一轮 ``_model_node`` 写入的 ``AIMessage.tool_calls``
    配对，防止悬空 ``tool_calls`` 触发 OpenAI 协议校验失败。

    参数:
        operations: 领域操作门面，提供 ``append_runtime_message`` 增量落库。
        obs_messages: 本批次工具观察 ``RuntimeMessage`` 列表（已含 ``tool_call_id`` 元数据）。

    返回:
        无。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 落库失败时由 ``append_runtime_message`` 透传，
        此时后续消息不写回（保持 DB 与上下文一致：DB 失败则上下文也不写）。
        IndexError / ValueError: ``runtime_to_langchain`` 转换异常时抛出，同样不落库、
        不写回，避免 DB 与上下文撕裂；异常携带 ``tool_call_id`` 与批次进度写入 error 日志。

    副作用:
        逐条先把 ``obs_message`` 转为 LangChain 消息（转换失败则在落库前抛出，不污染
        DB），再调用 ``operations.append_runtime_message`` 落库，最后调用
        ``_runtime_context().add_message`` 将转换后的观察消息写回上下文。
    """
    total = len(obs_messages)
    for index, obs_message in enumerate(obs_messages):
        try:
            langchain_obs = runtime_to_langchain([obs_message])[0]
            operations.append_runtime_message(obs_message)
            _runtime_context().add_message(langchain_obs)
        except (sqlalchemy.exc.SQLAlchemyError, IndexError, ValueError, KeyError) as exc:
            # 仅捕获可预期的落库/转换/上下文异常；其他异常（如编程错误）直接抛出不被吞。
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


async def _tools_node(state: ReactGraphState) -> dict:
    """ReAct 工具节点：在权限审批后执行工具并把观察结果追加回上下文。

    节点按 ``RuntimeConfig.approval_resolver`` 决定是否需要审批：

    - 存在 ``approval_resolver``：用 ``interrupt()`` 暂停 graph 等待审批，审批结果
      （批准的工具调用列表）通过 ``Command(resume=)`` 恢复；随后执行工具。
    - 不存在 ``approval_resolver``（``None``）：视为「自动放行全部调用」，
      **不经过 ``interrupt()``**，直接以 ``state.pending_tool_calls`` 作为已批准列表执行工具。
      这样 graph 不会暂停，编排层循环可正常走到终态，避免「无审批器时反复
      interrupt→resume 同一工具调用」的死循环。

    工具执行通过 ``RuntimeOperations`` 完成，工具生命周期事件经 ``write_event`` 回调写入
    自定义事件流；观察消息由 bridge 转为 ``BaseMessage`` 存回 state。状态写入 **turn**。

    本节点为 ``async``，工具批次执行经 ``asyncio.to_thread`` 移出事件循环线程：
    ``execute_terminal`` 会同步阻塞至命令结束（最长 ``max_command_timeout``），
    若在事件循环线程内直跑，会连带卡死 SSE 推送与全部并发请求，运行期增量
    也就无从实时到达客户端。工作线程内的事件写入使用**闭包捕获的 writer**
    （见 ``_make_write_event``），因为 ``get_stream_writer()`` 依赖 LangGraph 的
    运行上下文，需在协程内先取出再带入线程。

    参数:
        state: 当前 graph state，含待执行工具调用。

    返回:
        需要合并回 graph state 的增量。其中 ``messages`` 为 ``list[BaseMessage]``
        （经 ``runtime_to_langchain`` 转换后的 LangChain 消息），与其他节点返回的
        消息类型保持一致；取消分支同样返回转换后的占位 ``ToolMessage``。

    副作用:
        - 经 ``_persist_tool_observations`` 把本批次工具观察消息逐条增量落库，并同步
          ``_runtime_context().add_message`` 写回运行时上下文，闭合上一轮 ``_model_node``
          写入的 ``AIMessage.tool_calls`` 配对（含被取消时的占位 ``ToolMessage``）；
        - 被取消分支写回占位后 ``return``，正常分支写回真实观察后继续；
        - 工具生命周期事件经 ``write_event`` 透传；状态写入 **turn**。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作
    task = operations.get_current_task()  # 任务（工具执行需要 task_id）
    turn = operations.get_current_turn()  # 当前 turn 记录

    tool_calls = state.pending_tool_calls  # 来自 model 节点写入的待执行工具调用
    step_id = f"step-{state.step_count}"  # 复用上一步 step_id（工具是 model 步的延续）

    # 无审批器（含字段缺失的测试桩）→ 自动放行，不暂停 graph，
    # 直接用原始 tool_calls 作为已批准列表。
    if getattr(rc, "approval_resolver", None) is None:
        log.info(
            "tools_node_auto_approved",
            extra={
                "msg": (
                    f"无审批器，自动放行 {len(tool_calls)} 个工具调用"
                    f"（不暂停 graph），step_id={step_id}"
                ),
                "data": {"step_id": step_id, "pending_tool_count": len(tool_calls)},
            },
        )
        approved_dicts = tool_calls
    else:
        log.info(
            "tools_node_started",
            extra={
                "msg": f"工具节点开始执行，等待审批，step_id={step_id}",
                "data": {"step_id": step_id, "pending_tool_count": len(tool_calls)},
            },
        )
        # 核心：interrupt 暂停 graph，把待审批工具调用交出去；外部审批后用
        # Command(resume=approved_list) 恢复，approved 即为恢复时传入的审批结果。
        approved = interrupt({"tool_calls": tool_calls})
        # 兼容两种恢复值：直接 list 用 list，否则（如误传）回退到原始 tool_calls。
        approved_dicts = tool_calls if not isinstance(approved, list) else approved

    # ★ 取消检查：审批恢复后（或自动放行时）、工具执行前，若 turn 已被取消则跳过工具执行
    if operations.is_current_turn_cancelled():
        log.info(
            "tools_node_cancelled",
            extra={
                "msg": f"工具节点恢复后检测到 turn 已取消，跳过工具执行，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        # 关键修复：跳过工具执行时，必须为 assistant 已写入 checkpoint 的 tool_calls 补占位
        # ToolMessage，否则下一轮拉回历史会出现悬空 assistant，触发 OpenAI 协议校验失败。
        placeholder_messages = [
            RuntimeMessage(
                role="tool",
                content_text=json.dumps(
                    {"content": "工具执行已被取消，未产生响应"}, ensure_ascii=False
                ),
                metadata={"tool_call_id": call.get("call_id", "")},
            )
            for call in state.pending_tool_calls
            if call.get("call_id")
        ]
        # 同步写回运行时上下文：占位 ToolMessage 必须写回，否则上一轮 _model_node 写回的
        # AIMessage 的 tool_calls 在上下文中悬空，下次模型节点 load_message() 拉出即触发
        # OpenAI 协议校验失败。
        _persist_tool_observations(operations, placeholder_messages)
        # 仅转换类型供 state reducer 消费，不再写回（写回已在上一行完成）。
        placeholder_langchain = runtime_to_langchain(placeholder_messages)
        return {
            "pending_tool_calls": [],
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "messages": placeholder_langchain,
        }

    # 把审批结果 dict 重建为内部 ToolCall 值对象（补全 arguments/call_id 默认值）。
    approved_calls = [
        ToolCall(
            tool_name=item["tool_name"],
            arguments=item.get("arguments") or {},
            call_id=item.get("call_id") or "",
        )
        for item in approved_dicts
    ]

    # 真正执行工具（内部会发工具生命周期事件，write_event 作为回调注入）。
    log.info(
        "tools_node_resumed",
        extra={
            "msg": f"审批已恢复，准备执行 {len(approved_calls)} 个工具调用，step_id={step_id}",
            "data": {"step_id": step_id, "approved_count": len(approved_calls)},
        },
    )
    # 在协程内取出 writer 并闭包捕获：工具批次在工作线程执行，线程内无法再依赖
    # get_stream_writer() 的运行上下文。同理，事件循环也需在此取出并传入，
    # 供命令运行期输出增量从工作线程调度回环广播。
    node_write_event = _make_write_event()
    tool_run = await asyncio.to_thread(
        operations.run_tool_calls,
        task.task_id,
        approved_calls,
        step_id,
        write_event=node_write_event,
        running_loop=asyncio.get_running_loop(),
    )
    observations = tool_run.observations  # 每个工具调用的观察结果
    # 逐条持久化本轮产生的工具观察消息，并同步写回运行时上下文
    # （替代 turn 结束后的批落库；写回使下一轮模型节点能看到工具结果）。
    # 落库与写回逐条配对：落库一条即写回一条，避免「部分落库、零写回」撕裂。
    _persist_tool_observations(operations, tool_run.messages_for_model)

    if operations.is_current_turn_cancelled():
        log.info(
            "tools_node_cancelled_after_execution",
            extra={
                "msg": f"工具批次执行后检测到 turn 已取消，停止后续模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        # 关键修复：取消时不能丢弃本批次已产生的工具响应消息，否则 checkpoint 中
        # assistant 的 tool_calls 将缺少对应 ToolMessage，导致下一轮拉回历史时触发
        # OpenAI 协议校验失败（"assistant message with tool_calls must be followed by
        # tool messages"）。已执行的工具（含被取消返回 error 的）其 observation 仍
        # 在 messages_for_model 中，必须回写 checkpoint 以闭合配对。
        return {
            "pending_tool_calls": [],
            "tool_error_count": state.tool_error_count,
            "terminal": True,
            "messages": runtime_to_langchain(tool_run.messages_for_model),
        }

    tool_error_count = state.tool_error_count  # 从 state 继承连续失败计数
    for observation in observations:
        if observation.status == "success":
            tool_error_count = 0  # 成功则清零（连续失败才累计）
        else:
            tool_error_count += 1  # 失败 +1

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
                "tool_error_count": tool_error_count,
            },
        },
    )

    if tool_error_count >= Settings.TOOL_ERROR_LIMIT:  # 连续工具错误达上限
        failed_turn = operations.fail_turn_if_running(
            turn.turn_id, end_reason="tool_error_limit_reached"
        )
        if failed_turn is None:
            log.info(
                "tools_node_error_limit_terminal_race_lost",
                extra={
                    "msg": (
                        f"工具错误上限失败落定时 turn 已非 running，"
                        f"跳过失败事件，step_id={step_id}"
                    ),
                    "data": {"step_id": step_id, "turn_id": turn.turn_id},
                },
            )
            return {
                "pending_tool_calls": [],
                "tool_error_count": tool_error_count,
                "terminal": True,
                # 即便达到错误上限也要回写本批次已产生的工具响应，否则 checkpoint 中
                # assistant 的 tool_calls 将缺对应 ToolMessage，下一轮拉回历史触发 OpenAI
                # 协议校验失败（"assistant message with tool_calls must be followed by
                # tool messages"）。
                "messages": runtime_to_langchain(tool_run.messages_for_model),
            }
        log.warning(
            "tools_node_error_limit",
            extra={
                "msg": f"连续工具错误达到上限，停止执行，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_error_count": tool_error_count,
                    "limit": Settings.TOOL_ERROR_LIMIT,
                },
            },
        )
        write_event(
            EventType.RUN_FAILED,
            RunFailedPayload(
                step_id=step_id,
                status="failed",
                error="tool_error_limit_reached",
                tool_name=observations[0].tool_name if observations else "",
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        return {
            "pending_tool_calls": [],
            "tool_error_count": tool_error_count,
            "terminal": True,  # 失败终态
            # 达到错误上限同样必须回写本批次工具响应，闭合 assistant 的 tool_calls，
            # 否则 checkpoint 悬空，下一轮拉回历史触发 OpenAI 协议校验失败。
            "messages": runtime_to_langchain(tool_run.messages_for_model),
        }

    return {
        "pending_tool_calls": [],  # 清空待执行工具调用
        "tool_error_count": tool_error_count,
        # 观察消息转 LangChain 消息追加进 state
        "messages": runtime_to_langchain(tool_run.messages_for_model),
    }
