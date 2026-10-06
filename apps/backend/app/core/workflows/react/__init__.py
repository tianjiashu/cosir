"""ReAct-like 工作流包，基于 LangGraph StateGraph 编排默认 Agent 执行策略。

子模块职责：
- ``state``：graph state 数据契约（仅承载控制流状态，消息通道由 ``RuntimeContextManager``
  独占管理，不进 state）。
- ``nodes``（``app.core.workflows.nodes``）：``model`` / ``tools`` / ``observe`` 节点行为
  （LangGraph 原生 callable）。
- ``edges``：把 state 的 ``next_node`` 转换成 LangGraph 条件边目标。
- ``workflow``：``ReactLikeWorkflow`` 编排入口（构建 graph、驱动执行、转发 custom stream）。
- ``runtime_config``：注入 ``config["configurable"]`` 的节点共享运行期依赖。
- ``streaming``：节点写入 custom stream 的中性模型增量契约。
"""

from app.core.workflows.react.edges import _route_target
from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState
from app.core.workflows.react.workflow import ReactLikeWorkflow

__all__ = [
    "ReactGraphState",
    "ReactLikeWorkflow",
    "ReactRoute",
    "_route_target",
]
