"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译包含 ``model`` / ``tools`` / ``wait_user`` /
``observe`` / ``structured_output`` 的 graph，以 LangGraph 状态流驱动执行；节点产生的模型、
工具和终态事实由 ``WorkflowOperations`` 写入 canonical conversation state，Transport 只订阅该事实。
graph 编译时挂既有 checkpointer，由 LangGraph 负责控制流状态持久化。

协作取消与用户输入等待通过 LangGraph ``interrupt`` 保留图断点；其中 human-in-the-loop 走
``tools → wait_user →（批准回 tools 真执行 / 其余进 observe）`` 的固定通路，用户的结构化决定
由续跑入口经 ``run(..., user_decisions=...)`` 传入并被 ``wait_user`` 消费；待决请求投影给前端与
挂起图都发生在 ``wait_user``；terminal checkpoint 在 ``run`` 的 ``finally`` 中收敛。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""

import asyncio
from collections.abc import Iterable, Sequence
from time import perf_counter
from typing import Any, cast

from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.runtime.execution_mode import ExecutionMode
from app.core.tools.schemas import UserDecision, build_resume_payload
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.workflow_operations import WorkflowOperations
from app.service.depends import get_terminal_session_service

from ..agent_workflow import AgentWorkflow, WorkflowRunFailure, build_checkpointer
from .edges import _route_target
from .runtime_config import RuntimeConfig
from .worflow_state.route import ReactRoute


def _reject_resume_without_decisions(parked_at_interrupt: bool, run_id: int) -> bool:
    """不带决定的续跑是否应被拒绝：图停在 human-in-the-loop 断点上且该 Run 未标记取消。

    ``goto model`` 会绕开 ``wait_user`` 的图断点，把待决请求静默丢掉，并让模型读到「等待确认」的
    占位结果行，所以默认拒绝。

    唯一例外是**用户取消等待中的 Run**：``ConversationRunExecutor.cancel`` 先标记取消信号、再以
    ``resume`` 模式重新驱动（否则挂起的图里没有任何节点会去读信号）。那条路径丢弃断点是预期行为
    ——取消本来就表示「这个请求不要了」，终态由 ``model_node`` 检出信号后落定。

    参数:
        parked_at_interrupt: 图是否停在某个 interrupt 上。
        run_id: 本次续跑的 Run 标识；用于查询进程内取消信号。

    返回:
        ``True`` 表示应拒绝本次续跑；``False`` 表示允许。

    异常:
        无。

    副作用:
        无（只读进程内取消集合）。
    """

    if not parked_at_interrupt:
        return False
    return not cancellation_registry.is_cancelled(run_id)


class ReactLikeWorkflow(AgentWorkflow):
    """基于"模型推理 -> 工具调用 -> 继续推理/最终回答"的默认工作流，由 LangGraph 编排。

    本类承担执行策略与运行期装配：按 Agent profile 自解析模型并绑定工具 schema、按 Task 取
    ``RuntimeContextManager`` 写本 Run 的 canonical 消息、自开 checkpointer 编译 graph；工具执行与
    Run 状态迁移经 ``WorkflowOperations`` 门面。Run 终态由节点在收束时落定，异常原样交给运行时收敛。
    """

    workflow_id = "react_like_v1"

    def __init__(self) -> None:
        """初始化工作流；本类不持有运行期状态，依赖都在 :meth:`run` 时经入参与门面注入。"""

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        ``model`` / ``tools`` / ``observe`` 三节点经条件边形成 ReAct 循环；每个工具批次都先经过
        human-in-the-loop 门 ``wait_user``：无待决请求时直通 ``observe``，有待决请求时经
        ``interrupt`` 挂起，用户批准后回到 ``tools`` 真正执行。graph 编译时挂入 ``checkpointer``
        以启用 graph 控制流持久化。普通协作取消仍由 ``model`` 节点的 ``interrupt`` 中断。

        参数:
            checkpointer: 已配置好的 LangGraph checkpointer。

        返回:
            已编译的 StateGraph。
        """

        # 延迟导入节点，打破 nodes 子包与 react 包之间的循环导入：
        # nodes.model_node -> react.state/runtime_config -> react.__init__
        # -> react.workflow -> nodes
        from app.core.workflows.react.nodes import (
            _model_node,
            _observe_node,
            _structured_output_node,
            _tools_node,
        )
        from app.core.workflows.react.nodes.wait_user_node import wait_user_node

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _model_node)
        builder.add_node("tools", _tools_node)
        builder.add_node("wait_user", wait_user_node)
        builder.add_node("observe", _observe_node)
        builder.add_node("structured_output", _structured_output_node)
        builder.add_edge(START, "model")
        # 超配额拦截收口在 model 节点（发起推理前 step_count > max_steps 直接终态）。
        builder.add_conditional_edges(
            "model",
            _route_target,
            {
                ReactRoute.TOOLS.value: "tools",
                ReactRoute.MODEL.value: "model",
                ReactRoute.STRUCTURED_OUTPUT.value: "structured_output",
                END: END,
            },
        )
        builder.add_edge("structured_output", END)
        # 工具批次先交给 human-in-the-loop 门：无待决请求时它直通 observe。这条边必须保持
        # **无条件直达**——待决请求的对外投影由 wait_user 在挂起前完成，中间插入节点或改成条件边
        # 会让用户在挂起期间看到没有内容的审批卡片（见 wait_user_node 的链路契约）。
        builder.add_edge("tools", "wait_user")
        # 用户批准后回 tools 真正执行；无待执行批准（含驳回 / 放弃）则交给 observe 收口。
        # 自环：恢复值没有覆盖全部待决请求时重新挂起剩余请求（LangGraph 不允许在同一节点执行内
        # 二次 interrupt，只能回到本节点产生新的节点执行）。
        builder.add_conditional_edges(
            "wait_user",
            _route_target,
            {
                ReactRoute.TOOLS.value: "tools",
                ReactRoute.OBSERVE.value: "observe",
                ReactRoute.WAIT_USER.value: "wait_user",
            },
        )
        # observe 判定后回 model 或结束工作流；工具结果观察的唯一收口在此。
        builder.add_conditional_edges(
            "observe",
            _route_target,
            {
                ReactRoute.MODEL.value: "model",
                END: END,
            },
        )
        return builder.compile(checkpointer=checkpointer)

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
        user_decisions: Sequence[UserDecision] | None = None,
    ) -> None:
        """驱动 ReAct graph 执行一次任务，直到完成、挂起或异常终止。

        以 LangGraph 状态流驱动已编译 graph：模型解析与工具绑定、运行期配置注入、上下文基线
        补写、graph 启动与流式消费、terminal checkpoint 收敛都在本方法内完成。本工作流不生产或
        透传通用 runtime event——Run 状态迁移与工具执行经 ``WorkflowOperations`` 提交，canonical
        消息由本方法装配的 ``RuntimeContextManager`` 写入，模型流式增量由 graph custom stream 经
        ``operations.process_event`` 转发到 snapshot。协作取消与通用用户输入等待都通过 LangGraph
        ``interrupt`` 保存 checkpoint。

        本方法**不收口异常**：模型解析失败与「续跑时 checkpoint 已结束」抛带稳定失败码的
        ``WorkflowRunFailure``，其余异常原样传播，统一由唯一调用方 ``AgentRuntime.run_agent``
        分类并落定 Run failed 终态；取消信号（``CancelledError``）不被捕获，继续向上走
        cancelled 路径。

        参数:
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新。
            callbacks: 可选 LangChain callbacks（如 Langfuse ``CallbackHandler``），注入
                ``graph.astream`` 的 ``config["callbacks"]`` 使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选 Langfuse trace 标识；由 runner 在启用 tracing 时注入。
                未启用 Langfuse 时为 None。
            execution_mode: 本次执行是 ``fresh`` 还是 ``resume`` / ``resume_with_input``；它
                决定注入 graph 的输入（``fresh`` 传初始 state；``resume`` 传
                ``Command(goto="model")`` 回退到 model 节点重跑，``resume_with_input`` 在
                ``wait_user`` 处于 interrupt 时传 ``Command(resume=...)``）以及上下文是否
                清空该 run 的旧条目（见 ``RuntimeContextManager.begin_run``）。
            user_decisions: 本次续跑携带的用户结构化决定；``resume_with_input`` 下若恢复现场的
                图状态里存在处于 interrupt 的任务，就把它作为 ``Command(resume=...)`` 的载荷注入
                （当前只有 ``wait_user`` 会把 Run 置为等待用户输入）。为空时注入空决定集合，由
                ``wait_user`` 重新挂起等待用户输入。

        返回:
            无。图正常收束或在等待节点处挂起后返回，interrupt 与 checkpoint 由 LangGraph 持久化。

        异常:
            WorkflowRunFailure: 运行期模型解析失败，或续跑时 checkpoint 已走到 END 无法续跑。
            Exception: 上下文写入失败、graph 构建 / 执行失败时原样传播，由
                ``AgentRuntime.run_agent`` 的异常边界落定 Run failed 终态。
            asyncio.CancelledError: 不被捕获，保留取消语义向上传播。

        副作用:
            解析并绑定模型；构造运行期配置与 task 级上下文管理器并补写本 Run 的 user 基线
            消息；开启 checkpointer 编译 graph 并流式驱动；退出前在 ``finally`` 中关闭本 Run
            的 terminal 并把 checkpoint 里的活跃会话投影收敛为 closed。
        """

        run = operations.get_current_run()
        run_id = run.id
        current_task = operations.get_current_task()

        agent_profile = operations.agent_profile
        try:
            resolved_model = resolve_chat_model(agent_profile=agent_profile)
            base_model = resolved_model.model
        except Exception as exc:
            raise WorkflowRunFailure(
                Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE,
                "运行期模型解析失败",
            ) from exc
        # 绑定的是 operations.task_tool_schemas——按本 Run allows_tools 过滤后的结果（口径与偏差说明
        # 见 AgentRuntime._build_operations）。
        tool_schemas = operations.task_tool_schemas
        if len(tool_schemas) > 0:
            bound_model = base_model.bind_tools(
                tool_schemas,
                strict=True,
                parallel_tool_calls=True,
            )
        else:
            bound_model = base_model

        thinking_channel = "reasoning_content" if resolved_model.supports_thinking else ""

        runtime_config = RuntimeConfig(
            operations=operations,
            run=run,
            model=cast(Runnable, bound_model),
            structured_output=agent_profile.structured_output,
            start_time=perf_counter(),
            usage_stats=ConversationRunUsageStats(),
            langfuse_trace_id=langfuse_trace_id,
            thinking_channel=thinking_channel,
            execution_mode=execution_mode,
        )
        # task 级上下文管理器是 Agent 上下文的唯一运行时协调入口：首次执行时惰性创建，并由
        # task space 以弱引用缓存——本方法必须持有它直到 graph 执行结束，否则实例会被回收，
        # 下一次访问只能从持久化 context 重新加载。
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        runtime_context_manager = task_runtime_spaces.get_or_create(
            current_task.id
        ).get_context_manager(agent_profile=agent_profile)

        # 绑定本 Run：fresh 只按 run 清掉该 Run 的旧条目（同 Task 的历史条目保留，作为模型输入的
        # 前缀），resume 保留该 Run 已有条目。
        runtime_context_manager.begin_run(
            run,
            execution_mode,
        )
        # 本 Run 的初始 user 消息是 canonical 上下文事实，必须在 graph 启动前补齐：fresh 时
        # ``begin_run`` 已清空该 Run 的旧条目，这里写基线；resume 时该 Run 的消息通常已存在。
        # 是否「已存在」由方法内部按 canonical context 事实判定，**不能**用 ``execution_mode``
        # 代替：续跑在「上次崩于写入之前」时仍需补写。写入失败由异常向上收敛为 Run 失败。
        runtime_context_manager.ensure_run_user_message(
            run.input_text,
            run.image_paths,
            run.extra.display_text if run.extra is not None else run.input_text,
            run.extra.attachments if run.extra is not None else None,
        )

        config = {
            "configurable": {
                "thread_id": run.checkpoint_thread_id,
                "run_id": run_id,
                "runtime_config": runtime_config,
                # 与 runtime_config 同口径经 config 注入，不进入 graph state
                # （非 list 对象不兼容 state reducer）。
                "runtime_context": runtime_context_manager,
            },
            # LangChain callbacks（如 Langfuse CallbackHandler）经此注入模型调用追踪。
            "callbacks": callbacks or [],
        }

        async with (build_checkpointer() as checkpointer):
            graph = self._build_graph(checkpointer)
            try:
                # 初始 state 只填控制流字段：模型消息与 runtime context 都不进 state（前者归
                # RuntimeContextManager，后者经 config 注入）。
                input_state = ReactGraphState(
                    step_count=0,
                    tool_error_count=0,
                    next_node=ReactRoute.MODEL,
                    instruction="",
                    max_steps=agent_profile.max_steps,
                    final_text="",
                    last_tool_results={},
                    terminal_sessions={},
                )
                if execution_mode == "resume" or execution_mode == "resume_with_input":
                    # 续跑准入：必须先判图状态。图已走到 END 时 ``astream`` 既不产出事件也不
                    # 返回（协程永久挂起），因此必须先拒绝续跑；失败码随异常交给运行时收敛。
                    snapshot = await graph.aget_state(config)
                    if not snapshot.next:
                        raise WorkflowRunFailure(
                            Constant.Run.RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED,
                            "该 Run 的 checkpoint 已结束，无法续跑",
                        )
                    # 图是否停在 human-in-the-loop 的挂起点上（``wait_user`` 的 interrupt）。
                    parked_at_interrupt = any(task.interrupts for task in snapshot.tasks)
                    if execution_mode == "resume_with_input":
                        runtime_config.resuming_wait_user = parked_at_interrupt
                        input_state = (
                            Command(resume=build_resume_payload(user_decisions or ()))
                            if runtime_config.resuming_wait_user
                            else None
                        )
                    else:
                        # 图停在待用户决定的断点上，却用不带决定的 ``resume`` 续跑：``goto model``
                        # 会绕开该断点，把待决请求静默丢掉，并让模型读到「等待确认」的占位结果行。
                        # 因此显式失败，由调用方改用带决定的续跑。
                        #
                        # 唯一例外是「用户取消等待中的 Run」：``ConversationRunExecutor.cancel``
                        # 已先标记取消信号，再以本模式重新驱动，目的正是让被绕开的请求作废、由
                        # ``model_node`` 检出信号落 cancelled。此时丢弃断点是预期行为。
                        if _reject_resume_without_decisions(
                            parked_at_interrupt, runtime_config.run.id
                        ):
                            raise WorkflowRunFailure(
                                Constant.Run.RUN_FAILURE_CODE_WAITING_FOR_INPUT,
                                "该 Run 正在等待用户决定，请提交决定后续跑",
                            )
                        # 无用户决定的续跑一律回退到 model 节点重跑：模型侧断点（协作取消、
                        # 输出待续写）没有可恢复的输入，只能让模型看到当前上下文后重新决策；
                        # 图仍从既有 checkpoint 上继续，不新建线程。
                        input_state = Command(goto=ReactRoute.MODEL.value)

                async for mode, value in graph.astream(
                    input_state,
                    config,
                    stream_mode=["values", "custom"],
                ):
                    # values 只推进图；custom 携带模型 chunk 的中性增量，由本工作流
                    # 统一写入 snapshot。两者都不是 Agent context 的来源。
                    if mode != "custom":
                        continue
                    operations.process_event(value)
            finally:
                await self._finalize_terminal_checkpoint(
                    graph,
                    config,
                    run_id,
                    reason="run_execution_finished",
                )

    async def _finalize_terminal_checkpoint(
        self,
        graph: Any,
        config: dict[str, Any],
        run_id: int,
        *,
        reason: str,
    ) -> bool:
        """关闭 Run 的 terminal，并把 checkpoint 中的活跃终端元数据收敛为终态。

        terminal worker 的真实生命周期由进程内 registry 管理；本方法在 graph 执行的 ``finally``
        中通过仍打开的 checkpointer，把 checkpoint 里 ``running`` / ``starting`` 的终端投影标记为
        ``closed``，避免后续 resume 看到已经不存在的 PTY。失败只记日志并返回 False，不覆盖 Run
        已落定的业务终态；executor 仍会在更外层再次强制关闭 worker。

        参数:
            graph: 已编译、与本 ``config`` 匹配的 graph（复用同一 checkpointer）。
            config: 含 ``thread_id`` 的 LangGraph 配置。
            run_id: 当前 Run 标识，用于 Run 级 terminal 清理。
            reason: 写入终端元数据的关闭原因。

        返回:
            ``True`` 表示 Run 级 terminal 清理成功、且 checkpoint 已收敛（没有终端元数据时视为
            已收敛）；``False`` 表示其中任一步失败（已记日志），调用方据此跳过收敛统计。
        """

        try:
            await asyncio.to_thread(
                get_terminal_session_service().close_run_terminals,
                run_id,
                reason=reason,
            )
            snapshot = await graph.aget_state(config)
            terminal_sessions = snapshot.values.get("terminal_sessions")
            if not isinstance(terminal_sessions, dict):
                return True
            projected = {
                session_id: dict(metadata)
                for session_id, metadata in terminal_sessions.items()
                if isinstance(session_id, str) and isinstance(metadata, dict)
            }
            for metadata in projected.values():
                if metadata.get("status") in {"starting", "running"}:
                    metadata["status"] = "closed"
                    metadata["end_reason"] = reason
            await graph.aupdate_state(config, {"terminal_sessions": projected})
            return True
        except Exception:
            log.exception(
                "workflow_terminal_checkpoint_cleanup_failed",
                extra={
                    "msg": "terminal worker 或 checkpoint 终态收敛失败",
                    "data": {"run_id": run_id, "reason": reason},
                },
            )
            return False

    async def recover_orphaned_terminal_checkpoints(
        self,
        runs: Iterable[object],
        *,
        reason: str = "runtime_restarted",
    ) -> int:
        """扫描最近 Run 的 checkpoint 并关闭遗留的活跃 terminal 元数据。

        启动恢复发生在新的 backend 进程中，旧 PTY worker 不属于当前 registry；本方法仍
        通过统一 terminal cleanup 设置 Run closing fence，并在 checkpointer 生命周期内把
        最近 Run 的 ``starting`` / ``running`` terminal 投影收敛为 ``closed``。后续 resume
        会清除 fence 并创建新的 terminal，不会复用旧 PTY。

        参数:
            runs: 每个 task 最近一次 Run 的记录对象，需提供 ``id`` 与
                ``checkpoint_thread_id`` 属性。
            reason: 写入 terminal 元数据的关闭原因。

        返回:
            实际发现并收敛的 terminal session 数量。

        异常:
            checkpoint 读取或写入失败只记录日志并继续扫描其他 Run；主库恢复状态不受影响。

        副作用:
            读取并可能更新 LangGraph checkpoint；同步调用 terminal service 的 Run 级清理。
        """

        run_list = list(runs)
        if not run_list:
            return 0
        recovered_count = 0
        async with build_checkpointer() as checkpointer:
            graph = self._build_graph(checkpointer)
            for run in run_list:
                run_id = getattr(run, "id", None)
                thread_id = getattr(run, "checkpoint_thread_id", None)
                if not isinstance(run_id, int) or not isinstance(thread_id, str) or not thread_id:
                    continue
                try:
                    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                    terminal_sessions = snapshot.values.get("terminal_sessions")
                    active_count = sum(
                        1
                        for metadata in (
                            terminal_sessions.values()
                            if isinstance(terminal_sessions, dict)
                            else ()
                        )
                        if isinstance(metadata, dict)
                        and metadata.get("status") in {"starting", "running"}
                    )
                    if active_count == 0:
                        continue
                    finalized = await self._finalize_terminal_checkpoint(
                        graph,
                        {"configurable": {"thread_id": thread_id}},
                        run_id,
                        reason=reason,
                    )
                    if not finalized:
                        continue
                    recovered_count += active_count
                    log.info(
                        "orphaned_terminal_sessions_recovered",
                        extra={
                            "msg": "启动恢复已强制关闭最近 Run 的遗留 terminal",
                            "data": {
                                "run_id": run_id,
                                "thread_id": thread_id,
                                "session_count": active_count,
                                "reason": reason,
                            },
                        },
                    )
                except Exception:
                    log.exception(
                        "orphaned_terminal_sessions_recovery_failed",
                        extra={
                            "msg": "启动恢复扫描 Run terminal checkpoint 失败",
                            "data": {"run_id": run_id, "thread_id": thread_id},
                        },
                    )
        return recovered_count
