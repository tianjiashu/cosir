"""一次驱动的进程内依赖、用量快照和当前工具批次。"""

from dataclasses import dataclass, field

from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.core.llm_provider.openai_model import OpenAIChatModel
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.core.workflow.node_helper.user_input_decision_handlers import (
    UserInputDecisionHandlers,
)
from app.core.workflow.workflow_operations import WorkflowOperations


@dataclass
class RuntimeContext:
    """恢复时重新装配；等待批次仅在挂起时冻结进 Run progress。"""

    operations: WorkflowOperations
    model: OpenAIChatModel
    step_limit: int
    structured_output: StructuredOutputSpec | None = None
    usage_stats: ConversationRunUsageStats = field(default_factory=ConversationRunUsageStats)
    thinking_channel: str = ""
    user_input_decision_handlers: UserInputDecisionHandlers = field(
        default_factory=UserInputDecisionHandlers
    )
    batch: ToolBatch = field(default_factory=ToolBatch)
