"""ReAct 单次执行的临时数据；等待所需数据嵌入 Run.execution_data 持久化。"""

from typing import Any

from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic import BaseModel

from app.core.workflow.execution_state.react_route import ReactRoute
from app.core.workflow.node_helper.tool_call_lifecycle import ToolCallLifecycleManager


class ReactExecutionState(BaseModel):
    """节点间临时计算结果，不承载 Run 生命周期。
    普通续跑只恢复计数并从模型开始；等待恢复才读取待决调用与观察，禁止通用节点重放。"""

    step_count: int
    tool_error_count: int
    next_node: ReactRoute
    instruction: str = ""
    last_tool_results: dict[str, Any]
    calls:list[ChatCompletionMessageFunctionToolCallParam] = []
    tool_call_lifecycle: ToolCallLifecycleManager | None = None
