"""工作流节点辅助能力集合。

本包聚合 ReAct 各节点的共享辅助：用户决策处理、工具调用生命周期、用户决策投影、
运行时上下文作用域、请求指纹与流式分片状态机。公开符号在此统一再导出，调用方与
测试均从 ``app.core.workflow.node_helper`` 根导入，不依赖具体子模块路径。
"""

from app.core.workflow.node_helper.common import (
    RunStopped,
    execution_scope,
    get_stream_writer,
    route_state,
)
from app.core.workflow.node_helper.request_fingerprint import (
    log_messages_fingerprint,
    log_tools_fingerprint,
)
from app.core.workflow.node_helper.streaming_part_state_machine import (
    StreamingPartStateMachine,
)
from app.core.workflow.node_helper.tool_call_lifecycle import (
    SettlementResult,
    ToolCallLifecycleManager,
    ToolCallLifecycleRecord,
)
from app.core.workflow.node_helper.user_input_decision_handlers import (
    UserInputDecisionHandlers,
)
from app.core.workflow.node_helper.user_input_projection import (
    apply_decision_to_observations,
    extract_request,
    request_call_id,
)

__all__ = [
    "RunStopped",
    "execution_scope",
    "get_stream_writer",
    "route_state",
    "log_messages_fingerprint",
    "log_tools_fingerprint",
    "StreamingPartStateMachine",
    "SettlementResult",
    "ToolCallLifecycleManager",
    "ToolCallLifecycleRecord",
    "UserInputDecisionHandlers",
    "apply_decision_to_observations",
    "extract_request",
    "request_call_id",
]
