"""ReAct-like 工作流包，基于 LangGraph StateGraph 编排默认 Agent 执行策略。

子模块职责：
- ``state``：graph state 数据契约与追加式 reducer。
- ``nodes``：``model`` / ``tools`` 节点行为（LangGraph 原生 callable）。
- ``edges``：``should_continue`` 条件边路由。
- ``workflow``：``ReactLikeWorkflow`` 编排入口（构建 graph、驱动执行、翻译事件）。
"""

from app.core.workflows.react.edges import _should_continue
from app.core.workflows.react.nodes import _model_node, _tools_node
from app.core.workflows.react.state import ReactGraphState, _add_messages
from app.core.workflows.react.workflow import ReactLikeWorkflow

__all__ = [
    "ReactGraphState",
    "ReactLikeWorkflow",
    "_add_messages",
    "_model_node",
    "_should_continue",
    "_tools_node",
]
