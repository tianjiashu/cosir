"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.events import EventSink
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.models.task_message import NoticeBody, TaskMessage
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
    # 本轮准入的工具名；只在模型返回调用后拦截，模型绑定仍用 Task 固化的完整 schema。
    allows_tools: frozenset[str]
    task_space: TaskRuntimeSpace
    event_sink: EventSink
    # 本次执行的内部消息队列：调度方投递外部队列消息，节点投递 Run 内通知，prepare_input 取光。
    mailbox: RunMailbox

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)

    @classmethod
    def for_run(
        cls,
        run: ConversationRunRecord,
        task: TaskRecord,
        workspace: WorkspaceRecord,
        agent: AgentRuntimeConfig,
        mailbox: RunMailbox,
        *,
        event_sink: EventSink,
    ) -> RuntimeContext:
        """为单个 Run 装配运行上下文，并算出本轮的工具准入边界。

        ``allows_tools`` 以 Task 固化的工具名为基准、减去本 Run 的 ``ban_tools``，只供工具执行层在
        模型返回调用后做准入拦截。模型绑定始终使用 Task 固化的完整 schema，不得按本轮准入过滤，
        否则会改变同一 Task 的请求前缀并破坏缓存复用。``event_sink`` 由装配方注入（core 不依赖
        Assistant Transport）。

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
                    content=cls._tool_availability_notice(ban_tools, allows_tools),
                    notice_kind="tool_availability",
                )
            )
        )
        return cls(
            run=run,
            task=task,
            workspace=workspace,
            agent=agent,
            allows_tools=frozenset(allows_tools),
            task_space=task_space,
            event_sink=event_sink,
            mailbox=mailbox,
        )

    @staticmethod
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

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)
