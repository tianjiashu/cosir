"""ReAct-like 工作流包，基于 LangGraph StateGraph 编排默认 Agent 执行策略。

子模块职责：
- ``state``：graph state 数据契约（仅承载控制流状态，消息通道由 ``RuntimeContextManager``
  独占管理，不进 state）。
- ``nodes``：``model`` / ``tools`` / ``observe`` / ``pause`` 节点行为（LangGraph 原生
  callable）。
- ``edges``：``_should_continue`` / ``_after_tools`` / ``_after_observe`` / ``_after_pause``
  条件边路由。
- ``workflow``：``ReactLikeWorkflow`` 编排入口（构建 graph、驱动执行、翻译事件）。
"""

from app.core.workflows.react.edges import (
    _after_observe,
    _after_pause,
    _after_tools,
    _should_continue,
)
from app.core.workflows.react.state import ReactGraphState
from app.core.workflows.react.workflow import ReactLikeWorkflow

__all__ = [
    "ReactGraphState",
    "ReactLikeWorkflow",
    "_after_observe",
    "_after_pause",
    "_after_tools",
    "_should_continue",
]
