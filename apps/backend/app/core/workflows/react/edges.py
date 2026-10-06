"""ReAct 工作流的动态路由适配。"""

from langgraph.graph import END

from app.core.workflows.react.worflow_state.state import ReactGraphState


def _route_target(state: ReactGraphState) -> str:
    """返回节点写入 graph state 的动态下一步目标。

    动态业务决策由节点写入 ``next_node``；本函数只把该决策交给 LangGraph，固定转移由
    ``StateGraph`` 普通边表达。

    参数:
        state: 当前 graph state。

    返回:
        ``ReactRoute`` 对应的节点名；结束路由转换为 LangGraph 的 ``END`` 标记。

    异常:
        无；state 模型负责校验路由值。

    副作用:
        无。
    """

    return END if state.next_node.value == "end" else state.next_node.value
