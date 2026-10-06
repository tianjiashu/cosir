"""ReAct-like 工作流的 LangGraph 路由适配。

节点负责作出互斥的下一步决策；本模块只把 graph state 中的路由值转换为 LangGraph 目标，
不重新推断业务条件或检查 Run 状态。
"""

from langgraph.graph import END

from app.core.workflows.react.worflow_state.route import ReactRoute
from app.core.workflows.react.worflow_state.state import ReactGraphState


def _route_target(state: ReactGraphState) -> str:
    """把节点写入的路由值转换成 LangGraph 条件边目标。

    参数:
        state: 当前 graph state，``next_node`` 由上一个节点明确写入。

    返回:
        对应 graph 节点名；``ReactRoute.END`` 转为 LangGraph 的 ``END``。

    异常:
        ValueError: graph state 中的 ``next_node`` 不属于已声明的路由值。

    副作用:
        无。
    """

    route = ReactRoute(state.next_node)
    if route is ReactRoute.END:
        return END
    return route.value
