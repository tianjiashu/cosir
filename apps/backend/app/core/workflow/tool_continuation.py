"""Run 持久恢复数据与当前工具批次之间的唯一转换边界。"""

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam

from app.core.workflow.execution_state.tool_batch import ToolBatch, ValidToolCall
from app.models.conversation_run_progress import ToolContinuation


def freeze_tool_continuation(
    batch: ToolBatch, pending_call: ChatCompletionMessageFunctionToolCallParam
) -> ToolContinuation:
    """挂起前只保存重开目标调用与结算观察所需的数据。"""
    return ToolContinuation(pending_call=pending_call, observations=batch.observations)


def restore_tool_batch(continuation: ToolContinuation) -> ToolBatch:
    """待决调用在挂起前已有卡片；恢复时只重开该调用，不重放同批其它工具。"""
    pending = continuation.pending_call
    return ToolBatch(
        calls=[pending],
        observations=list(continuation.observations),
        valid_tool_calls={
            pending["id"]: ValidToolCall(name=pending["function"]["name"], projection="projected")
        },
    )
