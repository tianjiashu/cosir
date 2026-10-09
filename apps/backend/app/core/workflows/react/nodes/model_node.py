"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``run`` / ``model``，将模型文本与 reasoning 增量写入 workflow custom
stream；用 ``model.astream()`` 消费流式输出（草稿由 ``RuntimeContextManager`` 累积并收口成完整
``AIMessage``），根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大
步数终止。run 状态变更经 ``WorkflowOperations`` 落到 ``ConversationRun``（唯一事实源）。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_content`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支），但模型这段说明并非丢弃——它会经 canonical conversation
facts 写入（``RuntimeContextManager.add_message_chunk`` 累积草稿，收口时经
``flush_message_chunk(mode="finalize")`` 原地固化为 canonical 消息）落库进历史上下文，并随 state
``instruction`` 字段下传给 ``tools`` / ``observe`` 节点，使下游执行与错误排查能看到模型
当时的意图。

模型侧数据处理辅助（流式 chunk 解析 ``ModelChunkProcessor``、流式 part 生命周期）
已拆为独立模块，本模块仅 import 使用；节点共享运行时原语见 ``common``。
"""
import asyncio

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
)
from langchain_core.messages.tool import ToolCall
from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.workflows.react.node_helper.common import (
    _runtime_config,
    _runtime_context,
    route_state,
)
from app.core.workflows.react.node_helper.finalize_max_steps import _finalize_max_steps
from app.core.workflows.react.node_helper.model_chunk import ModelChunkProcessor
from app.core.workflows.react.node_helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.core.workflows.react.node_helper.tool_call_lifecycle import ToolCallLifecycleManager
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.vision_input import resolve_messages_for_model
from app.utils.message_content import content_to_text


def _build_continuation_prompt(finish_reason: str | None) -> str | None:
    """为未完成的模型输出构造一次模型可消费的继续提示。

    只区分两类：``length`` 类原因（``Constant.Workflow.CONTINUATION_FINISH_REASONS``）表示本轮
    达到输出上限，已有文本可被模型自然延续，**不注入提示**（返回 ``None``）；缺失或未知原因意味着
    Provider / 适配器没有给出可确认的正常结束信号，必须显式追加继续提示，否则模型无从得知上一轮
    未完成。

    两种情况下已有文本都不应被当作最终回答；是否注入提示完全由本函数的返回值决定。

    参数:
        finish_reason: 已归一化的 Provider 完成原因，可为 ``None``。

    返回:
        追加到 canonical context 的 ``SystemMessage`` 文本；``length`` 类截断返回 ``None``，
        表示本轮无需注入提示、直接由调用方路由回模型节点。
    """

    if finish_reason in Constant.Workflow.CONTINUATION_FINISH_REASONS:
        return None
    reason_text = finish_reason or "missing"
    return (
        "Your previous response did not provide a recognized completion signal "
        f"(finish_reason={reason_text}). Treat it as incomplete, continue from where it stopped, "
        "do not repeat completed content, and finish the answer."
    )


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``run`` / ``model``，把模型文本与 reasoning 增量写进
    workflow custom stream；模型草稿由 ``RuntimeContextManager`` 累积并收口成完整 ``AIMessage``，
    节点据此选择工具分支、最终回答分支，或因无效输出 / 超过最大步数终止。Run 状态变更一律经
    ``WorkflowOperations`` 落到 ``ConversationRun``（唯一事实源），本节点不直接写库。

    节点必须在 graph 运行上下文内执行：stream writer 与运行时配置都取自 LangGraph 注入的
    config（见 ``node_helper.common``）。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、路由标志、本轮工具调用生命周期、说明文本等）；
        本次推理已超配额（``step_count > max_steps``）时不发起推理，直接返回
        ``_finalize_max_steps`` 的终态 state patch。

    异常:
        RuntimeError: 超步数收口时 ``RuntimeConfig`` 未携带 run id（见 ``_finalize_max_steps``）；
            或本步模型未产出任何 chunk（无聚合消息，也就无草稿可收口）。
        Exception: 模型调用或流式消费失败时原样上抛；两者都由 ``AgentRuntime.run_agent`` 的异常
            边界收敛为 Run failed 终态。

    副作用:
        - 起草与收口：经 ``RuntimeContextManager.add_message_chunk`` 增量持久化本轮 assistant 草稿；
          正常结束后用最后一条聚合消息经 ``ToolCallLifecycleManager.classify`` 修订工具调用，再经
          ``flush_message_chunk(mode="finalize")`` 一次收口为 canonical 消息（同一行、同一序号，
          不新增行），使下一模型步能读到本轮输出；
        - 流式投影：文本与 reasoning 增量经 LangGraph custom stream 写给 workflow，由 workflow
          统一交给 ``WorkflowOperations.process_event`` 更新 snapshot；
        - 协作取消：检测点只有两处——「进入模型请求前」与「流式循环内每个 chunk 处理前」，两处的
          收口动作不同：请求前命中时本步还没有草稿、``tool_call_lifecycle`` 也尚未重建，只做
          ``WorkflowOperations.cancel_run_if_running`` 落定取消终态后 ``interrupt``；流式循环内命中时
          先 ``flush_message_chunk(mode="cancel")`` 收口草稿（落库但不加入模型上下文，半截消息不会
          进入下一次请求）、``parts.finish()`` 关闭当前 part、经 ``ToolCallLifecycleManager.cancel``
          把已投影的工具调用收为 ``cancelled`` 以关闭前端 part（``cancel`` 只改记录状态，本身不阻止
          派发），最后落定取消终态。
          两处都以 ``interrupt`` 收尾：节点不返回 state patch、不写路由，本批（含半截）调用因此不会
          进入 ``tools``，且图不结束，该 run 仍可续跑。循环退出后的收口不再检查取消，因此取消若落在
          「最后一个 chunk 处理完 → 循环退出」之间，本轮输出会照常收口并按正常路径路由（该批工具仍
          会执行），下一次进入本节点时才由请求前检查挂起；
        - 工具调用归桶：``classify`` 把已解析 ``tool_calls`` 与未解析 ``invalid_tool_calls`` 合并后
          按 ``id`` 逐个裁决——已注册但不在本轮 ``allows_tools`` 的进 ``blocked_calls``，其余进
          ``valid_calls``（``id`` 不可用的解析噪声直接丢弃）。随后本节点清空
          ``ai_message.invalid_tool_calls``，并按 ``blocked_calls + valid_calls`` 重写
          ``ai_message.tool_calls``，使模型协议里每条调用都有配对身份；它们的执行与 ``ToolMessage``
          回写由 ``tools`` / ``observe`` 节点完成；
        - 无工具调用时：只有 Provider 明确报告正常完成原因才标记最终回答；长度截断直接路由回模型
          节点由模型自行延续，缺失或未知完成原因还会额外追加一次继续提示（见
          ``_build_continuation_prompt``）。
    """

    rc = _runtime_config()
    operations = rc.operations
    model = rc.model
    thinking_channel = rc.thinking_channel
    chunk_processor = ModelChunkProcessor(thinking_channel)
    stream_writer = get_stream_writer()

    task_id = operations.get_current_task().id
    run_id = operations.get_current_run().id

    step_count = state.step_count + 1
    # 超配额拦截：不再发起推理，直接收口为 max_steps_reached 失败终态。
    if step_count > state.max_steps:
        return await _finalize_max_steps(state, step_count=step_count)
    step_id = f"step-{step_count}"
    if operations.is_current_run_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 run 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": rc.run.id},
            },
        )
        operations.cancel_run_if_running(usage_stats=rc.usage_stats, final_output="user_cancelled")
        interrupt({"reason": "user_cancelled"})

    workspace_id = operations.get_current_workspace().id
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    task_space = task_runtime_spaces.get_or_create(task_id)
    # load_message() 出口已归一化 assistant 消息，此处直接取用，不再重复 sanitize。
    messages: list[BaseMessage] = _runtime_context().load_message()

    # 延迟系统消息由其它层（tools / observe / 配置变更）在本 Run 期间投递，统一在这里写入上下文。
    # 归属过滤由 ``take_deferred_system_messages(run_id=...)`` 在取队列时完成（属于其它 Run 的消息
    # 在此被丢弃），此处不再重复判定；本地 messages 必须同步追加，否则本次模型请求看不到它。
    for system_message in task_space.take_deferred_system_messages(run_id=run_id):
        _runtime_context().add_message(system_message)
        messages.append(system_message)

    messages = await asyncio.to_thread(
        resolve_messages_for_model,
        messages,
        workspace_id=workspace_id,
    )

    log.info(
        "model_node_started",
        extra={
            "msg": f"模型节点开始执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": step_count,
                "message_count": len(messages),
            },
        },
    )
    # part 生命周期收口：把交错的 text/reasoning 流转换为顺序括号化的 part 事件流。
    parts = StreamingPartStateMachine(
        stream_writer, task_id=task_id, run_id=run_id, step_id=step_id
    )

    # lifecycle 只覆盖本次 model request；model -> tools -> observe 之间会沿 state 传递，
    # 下一次进入 model 时从空快照开始，避免混入上一轮已结束的 tool call。
    state.tool_call_lifecycle = ToolCallLifecycleManager(
        allows_tools=tuple(operations.allows_tools)
    )
    tool_call_lifecycle = state.tool_call_lifecycle

    # ``add_message_chunk`` 每次返回「累积至今」的聚合消息，循环结束后最后一次赋值即本轮完整输出；
    # 收口时直接用它做工具调用修订，无需再从草稿状态里取回。
    aggregated_message: AIMessage | None = None
    async for chunk in model.astream(messages):
        aggregated_message = _runtime_context().add_message_chunk(
            chunk, stream_id=step_id, run_id=run_id
        )

        if operations.is_current_run_cancelled():
            log.warning(
                "model_node_cancelled_usage_summary",
                extra={
                    "msg": f"模型流式因取消提前终止，本轮已消耗 token 摘要，step_id={step_id}",
                    "data": {"step_id": step_id, "usage": rc.usage_stats.to_dict()},
                },
            )
            _runtime_context().flush_message_chunk(
                stream_id=step_id, run_id=run_id, mode="cancel"
            )
            parts.finish()
            tool_call_lifecycle = tool_call_lifecycle.cancel(
                task_id=task_id, run_id=run_id, step_id=step_id
            )
            operations.cancel_run_if_running(
                usage_stats=rc.usage_stats, final_output="user_cancelled"
            )
            interrupt({"reason": "user_cancelled"})

        text = content_to_text(chunk.content)
        reasoning = chunk_processor.extract_reasoning(chunk)
        # 两个通道的空白处理刻意不同：文本保留空白分片（模型常把 "\n" 单独发成一个 chunk，丢弃会
        # 让前端正文少行），reasoning 只发有实义内容的帧（纯空白不出事件）。
        if text:
            parts.text(text)
        if reasoning and reasoning.strip():
            parts.reasoning(reasoning)
        # 传入的是本步累积后的 AIMessage（``add_message_chunk`` 的返回），而非原始
        # chunk：``extract_tool_calls`` 按属性读取 ``tool_call_chunks`` / ``tool_calls``，
        # 两种形态都适用（聚合后调用通常已落在 ``tool_calls``）。
        raw_tool_calls = chunk_processor.extract_tool_calls(aggregated_message)
        if raw_tool_calls:
            # 一个 chunk 可能并行携带多个 tool call，逐条处理已有的 name/id 身份。
            parts.tool_call()
            tool_call_lifecycle = tool_call_lifecycle.create(
                task_id=task_id,
                run_id=run_id,
                step_id=step_id,
                raw_tool_calls=raw_tool_calls,
            )

    # 收口一次完成：用本轮最后一条聚合消息在 classify 修订 tool_calls / invalid_tool_calls 之后
    # 直接固化同一行——同一条 assistant 消息全流程只占一个 sequence，不会出现「未修订行 + 修订行」
    # 两行并存。空流（一个 chunk 都没有）没有草稿可收口，属不应发生的协议异常。
    parts.finish()
    if aggregated_message is None:
        raise RuntimeError("本步模型未产出任何 chunk，无草稿可收口")
    ai_message: AIMessage = aggregated_message

    finish_reason = chunk_processor.extract_finish_reason(ai_message)
    rc.usage_stats.add_usage_metadata(ai_message.usage_metadata)

    tool_call_lifecycle = tool_call_lifecycle.classify(
        tool_calls=ai_message.tool_calls,
        invalid_tool_calls=ai_message.invalid_tool_calls,
    )

    # invalid_tool_calls 只表示 LangChain 没能把参数解析成字典，既不代表「工具不存在」也不代表
    # 「工具不在本轮白名单」，因此不得原样进入下一次 provider 请求。这些调用已按 id 收进上面的归桶
    # 结果，下面用归桶结果重写 tool_calls，使模型协议里每条调用都有可配对的身份。
    ai_message.invalid_tool_calls = []
    ai_message.tool_calls = [
        ToolCall(name=call.tool_name, args=call.args, id=call.tool_call_id)
        for call in tool_call_lifecycle.blocked_tool_calls + tool_call_lifecycle.valid_tools
    ]
    # 一次收口修订版到草稿行占用的同一 sequence：canonical 上下文只此一份，下一模型步即可读到。
    _runtime_context().flush_message_chunk(
        stream_id=step_id, run_id=run_id, mode="finalize", message=ai_message
    )

    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "tool_count": len(tool_call_lifecycle.valid_calls),
                "blocked_count": len(tool_call_lifecycle.blocked_calls),
                "output_text_length": len(ai_message.content),
                "finish_reason": finish_reason,
            },
        },
    )

    if tool_call_lifecycle.has_call:
        # 有任意工具调用（合法或非法）都进 tools 节点：合法调用真正执行，非法调用由 observe 结算；
        # 工具结果是否回流 model 也由 observe 决定。``instruction`` 把模型同时给出的说明文本带给
        # 下游，使工具执行与错误排查能看到模型当时的意图。
        return {
            **route_state(step_count, ReactRoute.TOOLS),
            "instruction": ai_message.content if isinstance(ai_message.content, str) else "",
            "tool_call_lifecycle": tool_call_lifecycle,
        }

    if finish_reason in Constant.Workflow.NORMAL_FINISH_REASONS and ai_message.content:
        # 没有工具调用且 Provider 明确报告正常结束 → 普通最终文本，必要时继续走结构化收口。
        final_answer = ai_message.content if isinstance(ai_message.content, str) else None
        if rc.structured_output is not None and ai_message.content:
            # 普通最终答复作为结构化节点的上下文候选；这里只交接控制流，不提前完成 Run。
            return {
                **route_state(step_count, ReactRoute.STRUCTURED_OUTPUT),
                "instruction": "",
                "final_text": content_to_text(ai_message.content),
                "tool_call_lifecycle": tool_call_lifecycle,
            }
        completed_run = operations.complete_run_if_running(
            rc.usage_stats, final_output=final_answer
        )
        if completed_run is None:
            log.info(
                "model_node_final_response_terminal_race_lost",
                extra={
                    "msg": f"最终回复落定时 run 已非 running，跳过完成事件，step_id={step_id}",
                    "data": {"step_id": step_id, "run_id": rc.run.id},
                },
            )
            return route_state(step_count, ReactRoute.END)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
            },
        )
        return {
            **route_state(step_count, ReactRoute.END),
            "final_text": ai_message.content,
        }

    if finish_reason not in Constant.Workflow.NORMAL_FINISH_REASONS:
        # 已有文本不代表模型完成：例如 finish_reason=length 只说明本轮达到输出上限。
        # length 类截断不注入提示，直接回到模型节点由模型自行延续；缺失/未知完成原因才把
        # AIMessage 之后的 SystemMessage 作为下一模型步的显式续写指令。
        continuation_prompt = _build_continuation_prompt(finish_reason)
        if continuation_prompt is not None:
            task_space.defer_system_message(
                SystemMessage(content=continuation_prompt, additional_kwargs={"run_id": run_id})
            )
        log.warning(
            "model_node_output_requires_continuation",
            extra={
                "msg": "模型输出没有可接受的正常完成原因，回到模型节点继续",
                "data": {
                    "step_id": step_id,
                    "run_id": rc.run.id,
                    "finish_reason": finish_reason,
                    "output_text_length": len(ai_message.content),
                },
            },
        )
        return {
            **route_state(step_count, ReactRoute.MODEL),
            "instruction": "",
        }

    # 队列里仍有本 Task 的延迟系统消息（其它层投递的修复提示 / 提示词增量）：这些消息只在本节点
    # 入口被消费，直接判非法输出会把它们吞掉，因此先回流 model 再判。
    if task_space.has_deferred_system_messages():
        return {
            **route_state(step_count, ReactRoute.MODEL),
            "instruction": "",
        }

    log.warning(
        "model_node_invalid_output",
        extra={
            "msg": f"模型既未返回工具调用也无有效文本，判定为非法输出，step_id={step_id}",
            "data": {"step_id": step_id, "output_text_length": len(ai_message.content)},
        },
    )
    failed_run = operations.fail_run_if_running(
        end_reason="invalid_model_output",
        usage_stats=rc.usage_stats,
        final_output="模型既未返回工具调用也无有效文本，判定为非法输出",
    )
    if failed_run is None:
        log.info(
            "model_node_invalid_output_terminal_race_lost",
            extra={
                "msg": f"非法模型输出失败落定时 run 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "run_id": rc.run.id},
            },
        )
    return route_state(step_count, ReactRoute.END)
