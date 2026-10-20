"""ReAct 单次执行的临时数据；等待所需数据嵌入 Run.execution_data 持久化。"""

from pydantic import BaseModel

from app.core.workflow.execution_state.react_route import ReactRoute


class ReactExecutionState(BaseModel):
    """驱动游标；计数从 Run progress 恢复，路由只在当前驱动内有效。"""

    step_count: int
    tool_error_count: int
    next_node: ReactRoute
