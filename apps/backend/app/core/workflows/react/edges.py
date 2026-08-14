"""ReAct-like 工作流的 LangGraph 条件边。

本模块只承载路由逻辑，不依赖节点行为或编排细节。条件边根据 graph state 决定
流向 ``tools`` / ``observe`` / ``model`` 节点还是结束，是 ReAct 循环与终止的分支点。
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
    repair_requested = state.repair_requested == "true"
    if (repair_requested or state.requested_tool) and state.step_count >= state.max_steps:
        return "max_steps"
    if state.repair_requested == "true":
        return "model"
    if state.requested_tool:
        return "tools"
    return END


def _after_tools(state: ReactGraphState) -> str:
    """工具节点出口：正常执行后进入 observe 节点，取消/终态直接结束。

    ``observe`` 节点独立承载「观察工具结果」步骤（阶段二接入 LLM 观察推理）。取消或
    终态分支不进 observe，避免对无观察价值的终态多做一次推理。

    参数:
        state: 当前 graph state。

    返回:
        ``"observe"`` 表示进入观察节点；``END`` 表示工作流结束。
    """

    if state.terminal or state.final_response:
        return END
    return "observe"


def _after_observe(state: ReactGraphState) -> str:
    """观察节点出口：根据错误上限判定决定继续模型推理还是结束。

    参数:
        state: 当前 graph state（``observe`` 节点写回 ``terminal`` 与更新后的
            ``tool_error_count``）。

    返回:
        ``"model"`` 表示回到模型节点继续推理；``END`` 表示工作流结束（达错误上限）。
    """

    if state.terminal or state.final_response:
        return END
    return "model"
