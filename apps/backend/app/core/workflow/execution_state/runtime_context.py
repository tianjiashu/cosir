"""一次驱动的不可序列化依赖；只在 execution_scope 内生效，不写入 Run。"""

from dataclasses import dataclass, field

from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.llm_provider.openai_model import OpenAIChatModel
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.step_context import StepContext
from app.core.workflow.node_helper.user_input_decision_handlers import (
    UserInputDecisionHandlers,
)
from app.core.workflow.workflow_operations import WorkflowOperations
from app.models import ConversationRunRecord


@dataclass
class RuntimeContext:
    """一次驱动共享的运行依赖与用量累计；恢复时从 Run 重新装配。"""

    operations: WorkflowOperations
    run: ConversationRunRecord
    model: OpenAIChatModel
    max_steps: int
    structured_output: StructuredOutputSpec | None = None
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    thinking_channel: str = ""
    user_input_decision_handlers: UserInputDecisionHandlers = field(
        default_factory=UserInputDecisionHandlers
    )
    # 当前模型请求（step）的记账；``model_node`` 每次进入时整体替换为新实例，其内部集合因此
    # 天然只覆盖本请求，不随驱动跨 step 累积。
    step_context: StepContext = field(default_factory=StepContext)
