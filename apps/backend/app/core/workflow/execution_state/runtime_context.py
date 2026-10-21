"""一次 Run 驱动的 Run / Task 级事实与驱动期状态：节点经 ``_runtime_context()`` 读取。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.events import EventSink
from app.core.workflow.cancel.conversation_run_cancellation_registry import (
    cancellation_registry,
)
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.models import ConversationRunRecord, TaskRecord, WorkspaceRecord
from app.models.user_decision import UserDecision

if TYPE_CHECKING:
    from app.core.agents.agent_profile import AgentProfile
    from app.core.llm_provider.openai_model import OpenAIChatModel
    from app.task_runtime.task_runtime_space import TaskRuntimeSpace


@dataclass
class RuntimeContext:
    """每次驱动重新装配，不参与恢复：数据事实在 Run 进度与 Task context，等待批次只在挂起时
    冻结进 Run 进度。

    只承载随 Run / Task 变化的事实与对象；进程级单例（Run 状态写入、inbox、工具系统）由节点经
    ``app.service.depends`` / ``app.config.configuration`` 直接取用，不在此转手。事件只经
    ``event_sink`` 回调交出，core 因此不依赖 Assistant Transport；节点用 ``node_helper.emit``
    发事件，sink 失败在那里降级。后一组字段由 ``ReactLikeWorkflow`` 在解析模型、读取进度后补齐。
    """

    # —— 装配方注入：本次 Run 的事实 ——
    run: ConversationRunRecord
    task: TaskRecord
    workspace: WorkspaceRecord
    profile: AgentProfile
    # 本轮准入的工具名；只在模型返回调用后拦截，模型绑定仍用 Task 固化的完整 schema。
    allows_tools: frozenset[str]
    task_space: TaskRuntimeSpace
    event_sink: EventSink

    # —— workflow 驱动期补齐 / 可变状态 ——
    model: OpenAIChatModel | None = None
    thinking_channel: str = ""
    step_limit: int = 0
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    batch: ToolBatch = field(default_factory=ToolBatch)
    # 等待恢复入口装入的已绑定决定；wait_user 节点消费一次后清空，其余入口为 None。
    resume_decision: UserDecision | None = None

    def is_cancelled(self) -> bool:
        """本 Task 是否收到停止信号；只读进程内注册表，节点在多个检查点调用，不查库。"""
        return cancellation_registry.is_cancelled(self.task.id)
