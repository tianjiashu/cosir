"""ReAct-like 工作流的 LangGraph 条件边。

本模块只承载路由逻辑，不依赖节点行为或编排细节。条件边根据 graph state 决定
流向 ``tools`` 节点还是结束，是 ReAct 循环与终止的分支点。
"""

from langgraph.graph import END

from .state import ReactGraphState


def _should_continue(state: ReactGraphState) -> str:
    """条件边：根据 graph state 决定流向 tools 还是结束。

    参数:
        state: 当前 graph state。

    返回:
        ``"tools"`` 表示进入工具节点；``END`` 表示工作流结束。
    """

    if state.terminal or state.final_response:
        return END
    if state.requested_tool:
        return "tools"
    return END
