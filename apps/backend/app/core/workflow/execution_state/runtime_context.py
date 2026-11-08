"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from app.config.configuration import get_tool_registry
from app.core.events import EventSink
from app.core.tool.multi_agent.task_tool_scope import banned_derivation_tools
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    CancelReason,
    cancel_end_reason,
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.service.depends import get_conversation_event_projector
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

if TYPE_CHECKING:
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig
    from app.core.llm_provider.openai_model import OpenAIChatModel
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace


@dataclass
class RuntimeContext:
    """每次驱动重新装配，不参与恢复：数据事实在 Run 进度与 Task context。

    只承载随 Run / Task 变化的事实与对象；进程级单例（Run 状态写入、外部消息队列、工具系统）由节点经
    ``app.service.depends`` / ``app.config.configuration`` 直接取用，不在此转手。事件只经
    ``event_sink`` 回调交出，core 因此不依赖 Assistant Transport；节点用 ``node_helper.emit``
    发事件，sink 失败在那里降级。后一组字段由 ``ReactLikeWorkflow`` 在解析模型、读取进度后补齐。
    """

    # —— 装配方注入：本次 Run 的事实 ——
    run: ConversationRunRecord
    task: TaskRecord
    workspace: WorkspaceRecord
    # 本次 Run 的 Agent 运行时配置：Agent 定义与模型选择的最终解析，模型字段已完整。
    agent: AgentRuntimeConfig
    # 本 Run 准入的工具名；只在模型返回调用后拦截，模型绑定仍用 Task 固化的完整 schema。
    admitted_tools: frozenset[str]
    # 两个禁用来源（Task 级、Run 级）分开保留：工具准入通知要按来源说清「为什么不能用」。
    task_banned: frozenset[str]
    run_banned: frozenset[str]
    # 这次驱动之前 Task 的当前 Run 是什么样：新 Run 是上一个 Run，恢复是本 Run 迁回 running 前的
    # 记录（恢复会清空结束原因），唤醒为 None。只供驱动开始时推导通知（``drive_notices``）。
    preceding_run: ConversationRunRecord | None
    task_space: TaskRuntimeSpace
    event_sink: EventSink
    # 进入本次驱动的唯一通道：调度方投来的外部队列行副本，与驱动内通知（驱动开始时推导的工具准入
    # 与用户已停止、续写，经 ``mailbox.notify``，不落库）；prepare_input 取光。
    mailbox: RunMailbox

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    # 本次驱动起的标题支线（``branch.task_title``）；复用 ``model``，由 workflow 在关闭 client 前
    # 收尾。「本 Run 是否起过」看执行状态 ``title_requested``，不看这个字段。
    title_branch: asyncio.Task[None] | None = None

    @classmethod
    def for_run(
        cls, run: ConversationRunRecord, preceding_run: ConversationRunRecord | None
    ) -> RuntimeContext:
        """为单个 Run 的一次驱动装配运行上下文，算出本 Run 的工具准入边界。

        Task、工作区与 mailbox 取自 Run 所属 Task 的运行时空间（空间按 Task 只物化一次，这里不再
        重复读库）；Agent 配置由 ``AgentRuntimeConfig.for_run`` 合成，事件回调经
        ``app.service.depends`` 取 projector——core 因此不直接依赖 Assistant Transport。

        ``admitted_tools`` 以 Task 固化的工具名为基准，减去两个来源：Task 级（这类 Task 不允许的
        派生方式、派生深度已到上限）与 Run 级（本 Run 冻结的工具组按注册表展开，组名是用户意图的
        事实，展开在这里现算，前端不换算）。两者都按本 Run 现算，所以改
        ``AGENT_MAX_DERIVATION_DEPTH`` 对下一次 Run 即时生效——但只对 schema 里仍有派生工具的 Task
        （fork）有效，schema 已固化的子 Task 在创建时就没有那些工具声明（见 ``task_tool_scope``
        模块说明）。准入只供工具执行层在
        模型返回调用后拦截。模型绑定始终使用 Task 固化的完整 schema，不得按准入过滤，否则会改变同一
        Task 的请求前缀并破坏缓存复用。工具准入通知不在这里：它要对照上下文，由 workflow 在
        ``begin_run`` 之后推导（``drive_notices``）。
        """

        from app.core.agents.agent_runtime_config import AgentRuntimeConfig

        task_space = task_runtime_spaces.get_or_create(run.task_id)
        task = task_space.current_task
        task_tools = frozenset(task_space.task_tool_names)
        # 两者都先与 ``task_tools`` 求交——模型手里没有的工具不算「被禁」，通知不该提它。
        task_banned = banned_derivation_tools(task.task_type, task.derivation_depth) & task_tools
        run_banned = (
            frozenset(
                get_tool_registry().tool_groups_to_tool_names(run.options.disabled_tool_groups)
            )
            & task_tools
        )
        return cls(
            run=run,
            task=task,
            workspace=task_space.current_workspace,
            agent=AgentRuntimeConfig.for_run(run),
            admitted_tools=task_tools - task_banned - run_banned,
            task_banned=task_banned,
            run_banned=run_banned,
            preceding_run=preceding_run,
            task_space=task_space,
            event_sink=get_conversation_event_projector().process,
            mailbox=task_space.mailbox,
        )

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)

    def cancel_end_reason(self) -> str:
        """检查点检出停止后落库的结束原因（按信号来源）；只在 :meth:`is_cancelled` 为真后调用。"""
        return cancel_end_reason(cast(CancelReason, cancellation_registry.reason(self.task.id)))
