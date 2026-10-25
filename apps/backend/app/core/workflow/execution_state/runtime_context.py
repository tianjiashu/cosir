"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.events import EventSink
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.run_mailbox import RunMailbox
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord

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

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)
