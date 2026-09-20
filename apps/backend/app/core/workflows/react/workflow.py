"""默认 ReAct-like 工作流编排，由 LangGraph StateGraph 驱动。

本模块是工作流的唯一编排入口：构建并编译 graph（``model`` / ``tools`` / ``observe`` 三个节点
+ 条件边），以 LangGraph 状态流驱动图执行；节点产生的模型、工具和终态事实由
``RuntimeOperations`` 写入 canonical conversation state，Transport 只订阅该事实。
graph 编译时挂既有 checkpointer，由 LangGraph 负责控制流状态持久化。

协作取消是图内的唯一中断点：``model`` 节点落定取消终态后调用 ``interrupt`` 中断执行，图停在
带 interrupt 的任务上（``next`` 非空），因此该 run 仍可由本模块的续跑分支恢复。

节点行为见 ``nodes`` 模块，路由逻辑见 ``edges`` 模块，graph state 契约见 ``state`` 模块。
"""
from collections.abc import Iterable
from time import perf_counter
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.llm_provider.model_factory import resolve_chat_model
from app.core.llm_provider.model_failure import classify_model_failure
from app.core.runtime.checkpointer import build_checkpointer
from app.core.runtime.execution_mode import ExecutionMode
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.service.depends import get_terminal_session_service
from app.service.provider.capability_service import CapabilityService

from ...context.runtime_context_manager import RuntimeContextManager
from ..agent_workflow import AgentWorkflow
from .edges import _after_observe, _after_tools, _should_continue
from .runtime_config import RuntimeConfig
from .state import ReactGraphState


class ReactLikeWorkflow(AgentWorkflow):
    """基于“模型推理 -> 工具调用 -> 继续推理/最终回答”的默认工作流，由 LangGraph 编排。

    该类只承担执行策略职责，不直接创建模型、工具或数据库连接，所有外部能力都通过
    ``RuntimeOperations`` 注入。graph 编译时挂 ``AsyncSqliteSaver`` checkpointer，由 LangGraph
    负责控制流状态持久化。
    """

    workflow_id = "react_like_v1"

    def __init__(self) -> None:
        """初始化工作流。

        参数:
            无。
        """

    def _build_graph(self, checkpointer) -> Any:
        """构建并编译 ReAct StateGraph。

        ``model`` / ``tools`` / ``observe`` 三节点经条件边形成 ReAct 循环；graph 编译时挂入
        ``checkpointer`` 以启用 graph 控制流持久化。图内不设独立的暂停节点：协作取消由
        ``model`` 节点的 ``interrupt`` 中断（见 ``nodes.model_node``）。

        参数:
            checkpointer: 已配置好的 LangGraph checkpointer。

        返回:
            已编译的 StateGraph。
        """

        # 延迟导入节点，打破 nodes 子包与 react 包之间的循环导入：
        # nodes.model_node -> react.state/runtime_config -> react.__init__
        # -> react.workflow -> nodes
        from app.core.workflows.react.nodes import _model_node, _observe_node, _tools_node

        builder = StateGraph(ReactGraphState)
        builder.add_node("model", _model_node)
        builder.add_node("tools", _tools_node)
        builder.add_node("observe", _observe_node)
        builder.add_edge(START, "model")
        # 超配额拦截收口在 model 节点（发起推理前 step_count > max_steps 直接终态）。
        builder.add_conditional_edges(
            "model",
            _should_continue,
            {"tools": "tools", "model": "model", END: END},
        )
        # tools 执行后进入 observe；取消/终态分支直接 END，不进 observe 避免多余推理。
        builder.add_conditional_edges("tools", _after_tools, {"observe": "observe", END: END})
        # observe 判定后回 model 继续推理，或达错误上限终态 END。
        builder.add_conditional_edges("observe", _after_observe, {"model": "model", END: END})
        return builder.compile(checkpointer=checkpointer)

    @staticmethod
    def _write_stream_item(operations: WorkflowOperations, mode: str, value: object) -> None:
        """消费 workflow stream 中的模型增量并写入 snapshot。

        参数:
            operations: 当前 Conversation Run 的运行时操作门面。
            mode: LangGraph stream 模式；只有 ``custom`` 模式由本方法处理。
            value: custom stream 产出的中性模型增量。

        返回:
            无。

        异常:
            无。``operations.process_event`` 内部已把投影异常降级为日志，custom 增量结构
            非法不会中断工作流。

        副作用:
            将文本或 reasoning 增量交给 ``RuntimeOperations``，由其更新 snapshot 并发布
            Assistant Transport 增量；不会写入 Agent context。
        """

        if mode != "custom":
            return
        operations.process_event(value)

    @staticmethod
    def _failure_code_for(exc: BaseException) -> str:
        """把异常归类为失败 code；识别不出模型调用错误时用中性兜底 code。

        参数:
            exc: 待归类的异常。既可能是从 graph 逃逸的异常，也可能是本工作流构建期
                （模型解析、运行期配置构造）抛出并就地收口的异常。

        返回:
            ``ErrorKind`` 的模型错误分类值，或 ``RUN_FAILURE_CODE_GRAPH_FAILED``（无法判定时）。

        异常:
            无。

        副作用:
            无。
        """

        return classify_model_failure(exc) or Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED

    @staticmethod
    def _current_run_id(operations: WorkflowOperations) -> int | None:
        """读取当前 Run 标识用于日志；门面不可用时返回 ``None``。

        收口日志必须在**任何**情况下都写得出来：异常收尾路径上 ``operations`` 可能已不可用
        （例如落定失败后门面解绑）。此处把读取本身降级为 ``None``，避免日志语句反过来抛出
        新异常、把原始失败掩盖掉。

        参数:
            operations: 当前 Conversation Run 的运行时操作门面。

        返回:
            Run 标识；读取失败时返回 ``None``。

        异常:
            无。

        副作用:
            无。
        """

        try:
            return operations.get_current_run().id
        except Exception:  # 日志字段读取失败按「不知道」处理
            return None

    @staticmethod
    def _settle_failed_run(
        operations: WorkflowOperations,
        end_reason: str,
        *,
        usage_stats: ConversationRunUsageStats | None = None,
        final_output: str | None = None,
    ) -> None:
        """把本轮的 running Run 落定为 failed 终态。

        本方法是「graph 构建期与执行期异常逃逸」的唯一终态收口点：调用方负责在调用它之后
        继续向上抛出原异常，而 Run 终态必须先在此落定——否则异常逃逸到 runner / executor 后
        无人落终态，Run 会永久停留在 ``running``，前端既收不到失败原因也无法开始新轮次。

        参数:
            operations: 当前 Conversation Run 的运行时操作门面，提供 run 状态迁移入口。
            end_reason: 稳定失败 code（同时作为 ``conversation_runs.end_reason`` 与受控错误的
                ``code``）；模型调用异常请传 ``classify_model_failure`` 的结果。
            usage_stats: 可选累计用量；异常发生前已消耗的 token 随终态一并落库。
            final_output: 可选失败说明文本；为 ``None`` 时使用该 code 对应的受控用户文案，
                使委派场景的主 Agent 也能感知子 run 的失败原因。

        返回:
            无。

        异常:
            无。落终态失败（如数据库写入异常）只记 error 日志并返回——收尾失败不得替换调用方
            正在向上抛出的原始异常；Run 已由其它路径落终态时记 info 日志并返回。

        副作用:
            更新 ``conversation_runs`` 行（failed 终态、end_reason、受控错误、用量与 final_output）
            并发布 Run 状态事件；写 ``run_failure_settled`` / ``run_failure_settle_failed`` /
            ``run_failure_settle_race_lost`` 日志。
        """

        run_id = ReactLikeWorkflow._current_run_id(operations)
        resolved_output = run_failure_message(end_reason) if final_output is None else final_output
        try:
            failed_run = operations.fail_run_if_running(
                end_reason=end_reason,
                usage_stats=usage_stats,
                final_output=resolved_output,
            )
        except Exception:
            log.exception(
                "run_failure_settle_failed",
                extra={
                    "msg": "异常收尾时落定 failed 终态失败，run 可能仍为 running",
                    "data": {"run_id": run_id, "end_reason": end_reason},
                },
            )
            return
        if failed_run is None:
            log.info(
                "run_failure_settle_race_lost",
                extra={
                    "msg": "异常收尾落定 failed 时 run 已非 running，跳过终态写入",
                    "data": {"run_id": run_id, "end_reason": end_reason},
                },
            )
            return
        log.info(
            "run_failure_settled",
            extra={
                "msg": "异常已将 run 落定为 failed 终态",
                "data": {"run_id": failed_run.id, "end_reason": end_reason},
            },
        )

    async def run(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """执行一个任务，并在异常逃逸时把 Run 落定为 failed 终态。

        本方法只做「调用图执行 + 异常兜底收口」两件事：正常终态由节点内的
        ``WorkflowOperations`` 落定；任何逃逸出 ``_run_graph`` 的异常都在此处先落 failed 终态
        再原样抛出——runner 与 executor 都刻意不写终态，异常若直接逃逸，Run 会永久停留在
        ``running``，前端既收不到失败原因也无法开始新轮次。

        参数:
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新。
            callbacks: 可选 LangChain callbacks（如 Langfuse ``CallbackHandler``）。
            langfuse_trace_id: 可选 Langfuse trace 标识；未启用 Langfuse 时为 None。
            execution_mode: 本次执行是 ``fresh`` 还是 ``resume``。

        返回:
            无（协程）。

        异常:
            Exception: 原样重新抛出 ``_run_graph`` 的异常；抛出前 Run 已落 failed 终态。

        副作用:
            异常路径下把 Run 更新为 failed（含受控错误契约）并发布状态事件；其余副作用见
            ``_run_graph``。
        """

        try:
            await self._run_graph(operations, callbacks, langfuse_trace_id, execution_mode)
        except Exception as exc:
            # 兜底收口：图构建、resume 状态检查等路径的异常不经过 graph.astream 的 except
            # 分支，必须在这里补落终态。已在内部落定过的 run 只会命中 fail_run_if_running 的
            # 「已非 running」分支，记 info 日志后放行。
            self._settle_failed_run(operations, self._failure_code_for(exc))
            raise

    async def _run_graph(
        self,
        operations: WorkflowOperations,
        callbacks: list | None = None,
        langfuse_trace_id: str | None = None,
        execution_mode: ExecutionMode = "fresh",
    ) -> None:
        """驱动已编译 graph 执行一次任务，直到完成、失败、取消或达到最大步骤数。

        以 LangGraph 状态流驱动已编译 graph。工作流不再生产或透传 RuntimeEvent；模型、
        工具和终态事实由 ``RuntimeOperations`` 直接提交到 canonical conversation state，
        模型流式增量由 graph custom stream 统一转发到 snapshot。
        模型经 ``resolve_chat_model`` 构建（缺 Key 在构建期抛错）；
        工具由服务端工具策略直接执行，工作流不等待审批类外部决策；唯一的图内中断是协作取消
        触发的 ``interrupt``（见 ``model_node``）。

        参数:
            operations: 运行时操作门面，提供模型调用、工具执行、事件记录与状态更新。
            callbacks: 可选 LangChain callbacks（如 Langfuse ``CallbackHandler``），注入
                ``graph.astream`` 的 ``config["callbacks"]`` 使 LLM 调用被自动追踪。
            langfuse_trace_id: 可选 Langfuse trace 标识；由 runner 在启用 tracing 时注入，
                终态事件 payload 会携带该字段供前端展示。未启用 Langfuse 时为 None。
            execution_mode: 本次执行是 ``fresh`` 还是 ``resume``；它决定注入 graph 的输入
                （``fresh`` 传初始 state、``resume`` 传 ``None`` 或 ``Command(resume=...)``）
                以及上下文是否清空该 run 的旧条目（见 ``RuntimeContextManager.begin_run``）。

        返回:
            无（协程）。工作流只驱动领域事实写入；Transport 通过 canonical conversation
            state 订阅事实变更。

        异常:
            ValueError: Conversation Run 缺少 ``provider_id``——run 已先落 failed 终态。
            Exception: 模型解析失败或 graph 执行失败时，记 ``workflow_graph_failed`` /
                ``model_resolve_failed``，经 ``_settle_failed_run`` 落定 failed 终态后原样向上
                抛出；未被这些分支覆盖的异常再由外层 ``run`` 兜底落定。
        """

        run = operations.get_current_run()
        # 一个 Conversation Run 对应一个 LangGraph checkpoint thread。这里必须使用
        # run 创建时持久化的 UUID，而不是自增 run.id：主库可被清空/重建并重新分配同一个
        # 整数 ID，而 checkpoint 库可能仍保留旧状态；UUID 才能保证两套生命周期真正隔离。
        run_id = run.id
        current_task = operations.get_current_task()

        # Agent 执行主体
        agent_profile = operations.agent_profile
        # 构建模型：按 run.model_name 运行期兜底解析（None 时回退 agent_profile.model_name），
        # 失败记 ``model_resolve_failed`` 后抛出，由外层 graph.astream 异常分支收敛为 RUN_FAILED。
        try:
            base_model = resolve_chat_model(
                run=run,
                agent_profile=agent_profile,
            )
        except Exception as exc:
            log.exception(
                "model_resolve_failed",
                extra={
                    "msg": f"运行期模型解析失败，run 进入 RUN_FAILED：{exc}",
                    "data": {
                        "task_id": current_task.id,
                        "run_id": run.id,
                        "model": run.model_name or agent_profile.model_name,
                    },
                },
            )
            self._settle_failed_run(
                operations,
                Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE,
            )
            raise
        # 构建工具
        tool_schemas = [
            tool.to_model_tool_definition()
            for tool in operations.model_tools
            if agent_profile.allowed_tools is None or tool.name in agent_profile.allowed_tools
        ]
        try:
            bound_model = (
                base_model.bind_tools(tool_schemas, strict=True) if tool_schemas else base_model
            )
        except NotImplementedError:
            # 降级防御：模型不支持 bind_tools 时禁用工具继续运行（与缺 Key 无关，缺 Key 在
            # resolve_chat_model 构建期即抛错，不会走到这里）。
            log.warning(
                "model %s does not support bind_tools; running without tools "
                "(degraded: model does not implement tool binding)",
                type(base_model).__name__,
            )
            bound_model = base_model

        if run.provider_id is None:
            # 配置缺失也属于「本轮无法开始」的失败：先落 failed 终态再抛出，避免异常逃逸后
            # run 永久停留在 running。
            self._settle_failed_run(
                operations,
                Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE,
            )
            raise ValueError("Conversation Run provider_id is required")
        thinking_channel = CapabilityService.get_thinking_channel(run.provider_id)
        vision_input_format = CapabilityService.get_vision_input_format(run.provider_id)

        runtime_config = RuntimeConfig(
            operations=operations,
            run=run,
            model=cast(BaseChatModel, bound_model),
            start_time=perf_counter(),
            usage_stats=ConversationRunUsageStats(),
            langfuse_trace_id=langfuse_trace_id,
            thinking_channel=thinking_channel,
            thinking_roundtrip=True,
            vision_input_format=vision_input_format,
            execution_mode=execution_mode,
        )
        current_workspace = operations.get_current_workspace()
        # 构造 task 级运行时上下文（唯一事实源），注入 store 端口使 manager 成为消息
        # 读写唯一入口，并挂载上下文占用订阅者。
        runtime_context_manager = RuntimeContextManager.ensure_get_runtime_context_manager(
            agent_profile,
            current_workspace,
            current_task,
        )

        # 每个新 ConversationRun 都从 canonical history 建立 fresh 上下文。
        runtime_context_manager.begin_run(
            run,
            execution_mode,
            tool_schemas=tool_schemas,
        )
        # 本 Run 的初始 user 消息属于该 Run 的 canonical 上下文事实：fresh 时
        # ``begin_run`` 已清空该 Run 的旧条目，这里补写基线；resume 时同一 Run 的
        # user 消息已存在。是否「已存在」由方法内部按 canonical context 事实判定，
        # **不能**用 ``execution_mode`` 代替：续跑在「上次崩于写入之前」时仍需补写。
        # 必须在 graph 启动前完成，使首个 model 步的 ``load_message`` 能把用户输入交给
        # 模型；写入失败由异常向上收敛为 Run 失败。
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

        async with build_checkpointer() as checkpointer:
            graph = self._build_graph(checkpointer)
            # 初始 state 只填控制流字段：模型消息与 runtime context 都不进 state（前者归
            # RuntimeContextManager，后者经 config 注入）。
            initial_state = ReactGraphState(
                step_count=0,
                tool_error_count=0,
                requested_tool=False,
                continue_model=False,
                final_response=False,
                terminal=False,
                instruction="",
                max_steps=agent_profile.max_steps,
                final_text="",
                last_tool_results={},
            )
            # None 是 LangGraph 从既有 checkpoint 继续的明确语义；新的 dict 会启动
            # 一个新的 graph input，即使 thread_id 相同也不等价于 resume。
            # 因此 ``resume`` 分支要求 ``run.checkpoint_thread_id`` 指向的线程上已有
            # checkpoint：续跑**不可**轮换该字段，否则会落到一个空线程上无从继续。
            input_state: Any = initial_state
            if execution_mode != "fresh":
                # 续跑准入：必须先判图状态。图已走到 END 时 ``astream`` 既不产出事件也不
                # 返回（协程永久挂起），因此必须先拒绝并收敛该 run，再决定恢复输入。
                snapshot = await graph.aget_state(config)
                if not snapshot.next:
                    log.warning(
                        "workflow_resume_rejected_graph_finished",
                        extra={
                            "msg": "续跑被拒绝：该 run 的图已结束（无可执行节点）",
                            "data": {"task_id": current_task.id, "run_id": run_id},
                        },
                    )
                    self._settle_failed_run(
                        operations,
                        Constant.Run.RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED,
                    )
                    await self._finalize_terminal_checkpoint(
                        graph,
                        config,
                        run_id,
                        reason="resume_rejected_graph_finished",
                    )
                    return
                # 图停在带 interrupt 的任务上（当前唯一来源是 ``model_node`` 的协作取消）
                # 时必须用 ``Command(resume=...)`` 恢复；其余情况传 ``None``，语义为
                # 「从既有 checkpoint 继续」。
                input_state = (
                    Command(resume={"action": "resume"})
                    if any(task.interrupts for task in snapshot.tasks)
                    else None
                )
            try:
                async for mode, value in graph.astream(
                    input_state,
                    config,
                    stream_mode=["values", "custom"],
                ):
                    # values 只推进图；custom 携带模型 chunk 的中性增量，由本工作流
                    # 统一写入 snapshot。两者都不是 Agent context 的来源。
                    self._write_stream_item(operations, mode, value)
            except Exception as exc:
                log.exception(
                    "workflow_graph_failed",
                    extra={
                        "msg": "langgraph execution failed during workflow run",
                        "data": {
                            "task_id": current_task.id,
                            # 经 _current_run_id 读取：日志语句本身不得在收尾路径上抛出。
                            "run_id": ReactLikeWorkflow._current_run_id(operations),
                        },
                    },
                )
                # 图执行异常（模型调用报错、provider 不可用、节点内部硬错等）必须先落 failed
                # 终态再向上抛：本层是唯一知道异常形状的地方，而 runner / executor 都刻意不落
                # 终态，异常逃逸后 run 会永久停留在 running 且无法取消。
                self._settle_failed_run(
                    operations,
                    self._failure_code_for(exc),
                    usage_stats=runtime_config.usage_stats,
                )
                raise
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
        """关闭 Run 的 terminal 并把 checkpoint 中的活跃元数据收敛为终态。

        terminal worker 的真实生命周期由进程内 registry 管理；本方法只在 graph 仍持有
        checkpointer 时，把 checkpoint 中 ``running`` / ``starting`` 的终端投影标记为关闭，
        避免后续 resume 看到已经不存在的 PTY。checkpoint 写失败只记录日志，不覆盖 Run
        已经由 workflow 落定的业务终态；executor 仍会在更外层再次强制关闭 worker。
        """

        try:
            get_terminal_session_service().close_run_terminals(run_id, reason=reason)
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
                    snapshot = await graph.aget_state(
                        {"configurable": {"thread_id": thread_id}}
                    )
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
