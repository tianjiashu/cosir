"""驱动一次 Conversation Run：对话场景的装配方，也是 Run 失败的唯一异常边界。

``WorkflowSession`` 决定「什么时候跑」，本模块负责「怎么跑一次」：读取 Run、Task 与工作区，
合成本次 Run 的 Agent 运行时配置，把 Run / Task 级事实（Task 运行空间，以及 Transport
projector 的事件回调）装进 ``RuntimeContext``，再交给 core 的 ``ReactLikeWorkflow``。进程级单例
（Run 状态写入、外部消息队列、工具系统）由 workflow 节点直接取用，不经这里转手；事件回调必须由这里
注入，因为 core 不依赖 Assistant Transport。Run 生命周期与持久进度由领域服务维护，模型上下文
归 Task。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from app.core.workflow.workflow import ReactLikeWorkflow
from app.agent_team.node_profile import NODE_PROFILE_SNAPSHOT_KEY, build_node_profile
from app.agent_team.state.agent_team_run_state import AgentTeamRunRuntime
from app.config.configuration import get_agent_registry
from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.context.tool_call_closure import ContextProtocolError
from app.core.events import ToolCallsSettledEvent
from app.core.hook import HookContext, HookEvent, HookInterceptor
from app.core.llm_provider.model_failure import (
    classify_model_failure,
    extract_model_response_message,
)
from app.core.workflow.cancel.tool_call_cancellation_registry import tool_call_cancellation_registry
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.execution_state.runtime_context import RuntimeContext
from app.core.workflow.workflow_run_failure import WorkflowRunFailure
from app.models import ConversationRunRecord, ConversationRunStatus, TaskRecord, WorkspaceRecord
from app.models.conversation_run_failure import run_failure_message
from app.models.enums.conversation_run_status import RunTransition
from app.models.task_message import NoticeBody, TaskMessage
from app.service.depends import (
    get_conversation_event_projector,
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_model_config_service,
    get_task_service,
    get_workspace_service,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

_STOP_HOOK_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.CANCELLED.value,
        ConversationRunStatus.SUSPENDED.value,
    }
)


async def drive_run(
    run_id: int,
    mailbox: RunMailbox,
    initial_delivery: Callable[[], Awaitable[None]],
) -> None:
    """驱动一次 Run，并作为 Run 失败的唯一异常边界。

    初始投递 → 装配 Agent 运行时配置与运行上下文 → 驱动 workflow；正常终态由 workflow 节点落定。
    任何 ``Exception``（含初始投递与装配期）在这里分类并落定 failed，再把仍未收束的工具调用批量
    收束；Run 已落终态时条件迁移落空，不覆盖既有终态。落定自身失败时异常上浮给调度方记录。
    ``CancelledError`` 照常上浮（进程关闭），遗留 running Run 由下次启动收敛。

    ``mailbox`` 由调度方随执行创建并持续投递外部队列消息；``initial_delivery`` 是调度方的首次投递
    （等启动它的那批信号处理完才投递），必须在本边界内执行：它在边界外失败时 Run 会停在 running
    而没有执行者。

    副作用:
        触发 USER_PROMPT_SUBMIT 与 STOP（正常收束 / 取消 / 挂起）Hook；结束前回收本 Run 遗留的
        工具级取消信号（Task 级信号归调度方的释放边界）。
    """

    task_id: int | None = None
    run_state = get_conversation_run_state_service()
    try:
        run = await asyncio.to_thread(get_conversation_run_service().get_run, run_id)
        task_id = run.task_id
        await initial_delivery()
        task = get_task_service().get_task(task_id)
        workspace = get_workspace_service().get_workspace(task.workspace_id)
        agent = _runtime_config(run, task, workspace)
        # 无内置实现；Hook 判定 deny 也不阻断已开始的 Run。
        await HookInterceptor.async_safe_fire(
            HookContext.from_locatable(event=HookEvent.USER_PROMPT_SUBMIT, turn=run, locatable=None)
        )
        rc = _build_context(workspace, task, run, agent, mailbox)


        await ReactLikeWorkflow().run(rc)
        current = await asyncio.to_thread(get_conversation_run_service().get_run, run_id)
        if current.status in _STOP_HOOK_STATUSES:
            await HookInterceptor.async_safe_fire(
                HookContext.from_locatable(event=HookEvent.STOP, locatable=task, turn=run)
            )
    except Exception as exc:
        failure_code = _failure_code_for(exc)
        failed = run_state.transition(
            run_id,
            RunTransition.FAIL,
            end_reason=failure_code,
            final_output=run_failure_message(failure_code),
            error_message=extract_model_response_message(exc),
        )
        log.exception(
            "task_failed",
            extra={
                "msg": f"run 执行失败，失败 code: {failure_code}，错误类型: {type(exc).__name__}",
                "data": {
                    "task_id": task_id,
                    "run_id": run_id,
                    "failure_code": failure_code,
                    "error_type": type(exc).__name__,
                },
            },
        )
        if failed is not None:
            # 失败打断了工具执行时，pending / running 的工具调用不会再收到单条迁移。
            get_conversation_event_projector().process(
                ToolCallsSettledEvent(
                    task_id=failed.task_id,
                    run_id=run_id,
                    status="cancelled",
                    reason="runtime_failed",
                )
            )
    finally:
        # 兜底回收「点名了已结束的工具调用」这类不会再被消费的工具级信号。
        tool_call_cancellation_registry.clear_run(run_id)


def resolve_agent_config(run: ConversationRunRecord) -> AgentRuntimeConfig:
    """按 Run 的持久化事实合成其 Agent 运行时配置（规则见 :func:`_runtime_config`）。

    供 Run 之外的执行方复用父 Agent 配置，如用户批准 Agent Team 时为节点回落模型配置。
    """
    task = get_task_service().get_task(run.task_id)
    workspace = get_workspace_service().get_workspace(task.workspace_id)
    return _runtime_config(run, task, workspace)


def _runtime_config(
    run: ConversationRunRecord,
    task: TaskRecord,
    workspace: WorkspaceRecord,
) -> AgentRuntimeConfig:
    """合成本次 Run 独占的 Agent 运行时配置；注册表单例与 Team 冻结定义都不被修改。

    定义只由持久化事实推导：按 Run 的 Agent 身份从注册表解析（主 Agent 用系统作用域，其余用 Run
    所属工作区）；Team 节点 Task 再叠加其 ``extra`` 里冻结的节点快照。连接与能力字段只由本 Run 的
    ``model_config_id`` 物化——``api_key`` 不落盘。

    异常:
        RuntimeError: 注册表解析不到该 Agent，或 Run 未记录模型配置。
        KeyError: ``model_config_id`` 指向的配置已不存在。
        ModelSettingsError: 模型配置不完整。
    """
    agent_id = run.agent_id or "main_agent"
    workspace_scope = (
        AgentProfileRegistry.SYSTEM_WORKSPACE if agent_id == "main_agent" else workspace.root_path
    )
    definition = get_agent_registry().resolve(workspace_scope, agent_id)
    if definition is None:
        raise RuntimeError(f"agent profile unavailable for run {run.id}")
    node_snapshot = (task.extra or {}).get(NODE_PROFILE_SNAPSHOT_KEY)
    if node_snapshot is not None:
        definition = build_node_profile(
            definition, AgentTeamRunRuntime.model_validate(node_snapshot)
        )
    # Run 行来自持久化边界：建 Run 的唯一路径总会写入模型配置，缺失只可能是脏数据。
    if run.model_config_id is None:
        raise RuntimeError(f"run {run.id} has no model config")
    model_config = get_model_config_service().get_config(run.model_config_id)
    return AgentRuntimeConfig.resolve(
        definition, model_config, reasoning_effort=run.options.reasoning_effort
    )


def _failure_code_for(exc: BaseException) -> str:
    """把异常归类为 Run 失败 code。

    上下文完整性错误用本地 code，不归因于模型服务；``WorkflowRunFailure`` 已自带 code，不二次
    猜测；其余交给模型失败分类器，分类不出时回退中性 code，避免把普通执行错误误报成模型问题。
    """

    if isinstance(exc, ContextProtocolError):
        return Constant.Run.RUN_FAILURE_CODE_CONTEXT_PROTOCOL_INVALID
    if isinstance(exc, WorkflowRunFailure):
        return exc.failure_code
    return classify_model_failure(exc) or Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


def _build_context(
    workspace: WorkspaceRecord,
    task: TaskRecord,
    run: ConversationRunRecord,
    agent: AgentRuntimeConfig,
    mailbox: RunMailbox,
) -> RuntimeContext:
    """为单个 Run 装配 workflow 的运行上下文，并算出本轮的工具准入边界。

    ``allows_tools`` 以 Task 固化的工具名为基准、减去本 Run 的 ``ban_tools``，只供工具执行层在
    模型返回调用后做准入拦截。模型绑定始终使用 Task 固化的完整 schema，不得按本轮准入过滤，
    否则会改变同一 Task 的请求前缀并破坏缓存复用。

    副作用:
        向本次执行的 mailbox 投递本轮工具准入说明，由下一次 prepare_input 写入上下文。通知
        一旦消费即成为 canonical 历史的固定行，是后续 Run 请求前缀的一部分，不能改为「只注入
        本次请求、不落库」，否则下一个 Run 的前缀会从这里分叉。
    """
    task_space = task_runtime_spaces.get_or_create(task.id)
    ban_tools = set(run.options.ban_tools)
    allows_tools = {tool["name"] for tool in task_space.task_tool_schemas} - ban_tools
    mailbox.post(
        TaskMessage.of(
            NoticeBody(
                content=_tool_availability_notice(ban_tools, allows_tools),
                notice_kind="tool_availability",
            )
        )
    )
    return RuntimeContext(
        run=run,
        task=task,
        workspace=workspace,
        agent=agent,
        allows_tools=frozenset(allows_tools),
        task_space=task_space,
        event_sink=get_conversation_event_projector().process,
        mailbox=mailbox,
    )


def _tool_availability_notice(ban_tools: set[str], allows_tools: set[str]) -> str:
    """本轮工具准入说明；工具名排序，避免同一集合跨进程产生不同文本（前缀缓存）。"""
    if not ban_tools:
        return "Tool availability notice: all tools are available for this round."
    if not allows_tools:
        return "Tool availability notice: all tools are not available for this round."
    return (
        "Tool availability notice: the following tools are disabled for this round and calls "
        f"to them will be rejected: {', '.join(sorted(ban_tools))}. "
        f"{', '.join(sorted(allows_tools))} tools available."
    )
