"""一次 Run 驱动的 Run / Session 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from app.core.events import EventSink
from app.core.events.event_sink import event_sink
from app.core.tool.multi_agent.session_tool_scope import banned_derivation_tools
from app.core.workflow.cancel.run_cancellation_registry import (
    CancelReason,
    cancel_end_reason,
    cancellation_registry,
)
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.core.workflow.execution_state.run_usage_stats import RunUsageStats
from app.models import RunRecord, SessionRecord, WorkspaceRecord
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

if TYPE_CHECKING:
    from app.core.agents.agent_runtime_config import AgentRuntimeConfig
    from app.core.llm_provider.openai_model import OpenAIChatModel
    from app.session_runtime.session_runtime_space import SessionRuntimeSpace


@dataclass
class RuntimeContext:
    """每次驱动重新装配，不参与恢复：数据事实在 Run 进度与 Session context。

    只承载随 Run / Session 变化的事实与对象；进程级单例（Run 状态写入、外部消息队列、工具系统）由节点经
    ``app.service.depends`` / ``app.config.configuration`` 直接取用，不在此转手。事件只经
    ``event_sink`` 回调交出，core 因此不依赖 Assistant Transport；节点用 ``node_helper.emit``
    发事件，sink 失败在那里降级。后一组字段由 ``ReactLikeWorkflow`` 在解析模型、读取进度后补齐。
    """

    # —— 装配方注入：本次 Run 的事实 ——
    run: RunRecord
    session: SessionRecord
    workspace: WorkspaceRecord
    # 本次 Run 的 Agent 运行时配置：Agent 定义与模型选择的最终解析，模型字段已完整。
    agent: AgentRuntimeConfig
    # 本 Run 准入的工具名；只在模型返回调用后拦截，模型绑定仍用 Session 固化的完整 schema。
    admitted_tools: frozenset[str]
    # Session 固化工具里本 Run 不准入的那部分；工具准入通知据此告诉模型「为什么不能用」。
    banned_tools: frozenset[str]
    # 这次驱动之前 Session 的当前 Run 是什么样：新 Run 是上一个 Run，恢复是本 Run 迁回 running 前的
    # 记录（恢复会清空结束原因），唤醒为 None。只供驱动开始时推导通知（``drive_notices``）。
    preceding_run: RunRecord | None
    session_space: SessionRuntimeSpace
    event_sink: EventSink
    # 进入本次驱动的唯一通道：调度方投来的外部队列行副本，与驱动内通知（驱动开始时推导的工具准入
    # 与用户已停止、续写，经 ``mailbox.notify``，不落库）；prepare_input 取光。
    mailbox: RunMailbox

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    usage_stats: RunUsageStats = field(default_factory=RunUsageStats)
    # 本次驱动起的标题支线（``branch.session_title``）；复用 ``model``，由 workflow 在关闭 client 前
    # 收尾。「本 Run 是否起过」看执行状态 ``title_requested``，不看这个字段。
    title_branch: asyncio.Task[None] | None = None

    @classmethod
    def for_run(
        cls, run: RunRecord, preceding_run: RunRecord | None
    ) -> RuntimeContext:
        """为单个 Run 的一次驱动装配运行上下文，算出本 Run 的工具准入边界。

        Session、工作区与 mailbox 取自 Run 所属 Session 的运行时空间（空间按 Session 只物化一次，这里不再
        重复读库）；Agent 配置由 ``AgentRuntimeConfig.for_run`` 合成，事件回调经
        ``app.service.depends`` 取 projector——core 因此不直接依赖 Assistant Transport。

        ``admitted_tools`` 以 Session 固化的工具名为基准，减去这类 Session 不允许的派生方式与派生深度已到
        上限的派生工具。禁用集合按本 Run 现算，所以改 ``AGENT_MAX_DERIVATION_DEPTH`` 对下一次 Run 即时生效——但只对 schema 里仍有派生工具的 Session
        （fork）有效，schema 已固化的子 Session 在创建时就没有那些工具声明（见 ``session_tool_scope``
        模块说明）。准入只供工具执行层在
        模型返回调用后拦截。模型绑定始终使用 Session 固化的完整 schema，不得按准入过滤，否则会改变同一
        Session 的请求前缀并破坏缓存复用。工具准入通知不在这里：它要对照上下文，由 workflow 在
        ``begin_run`` 之后推导（``drive_notices``）。
        """

        from app.core.agents.agent_runtime_config import AgentRuntimeConfig

        session_space = session_runtime_spaces.get_or_create(run.session_id)
        session = session_space.current_task
        session_tools = frozenset(session_space.session_tool_names)
        # 先与 ``session_tools`` 求交——模型手里没有的工具不算「被禁」，通知不该提它。
        banned = banned_derivation_tools(session.session_type, session.derivation_depth) & session_tools
        return cls(
            run=run,
            session=session,
            workspace=session_space.current_workspace,
            agent=AgentRuntimeConfig.for_run(run),
            admitted_tools=session_tools - banned,
            banned_tools=banned,
            preceding_run=preceding_run,
            session_space=session_space,
            event_sink=event_sink(),
            mailbox=session_space.mailbox,
        )

    def is_cancelled(self) -> bool:
        """本 Session 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.session.id)

    def cancel_end_reason(self) -> str:
        """检查点检出停止后落库的结束原因（按信号来源）；只在 :meth:`is_cancelled` 为真后调用。"""
        return cancel_end_reason(cast(CancelReason, cancellation_registry.reason(self.session.id)))
