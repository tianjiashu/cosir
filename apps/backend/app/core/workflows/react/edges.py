"""ReAct-like 工作流的 LangGraph 条件边。

本模块只承载路由逻辑，不依赖节点行为或编排细节。条件边根据 graph state 决定
流向 ``tools`` / ``observe`` / ``model`` 节点还是结束，是 ReAct 循环与终止的分支点。
"""

from langgraph.graph import END

from .state import ReactGraphState


def _should_continue(state: ReactGraphState) -> str:
    """条件边：根据 graph state 决定流向 tools / model 还是结束。

    超配额拦截（``step_count > max_steps``）已提前到 ``model_node`` 发起推理前收口，
    本边不再承担 max_steps 路由，只区分继续动作。

    参数:
        state: 当前 graph state。

    返回:
        ``"tools"`` 进入工具节点；``"model"`` 表示继续推理（模型输出未以可接受原因结束
        时的续写回流）；``END`` 表示工作流结束（已终态或已产出最终回答）。
    """

    if state.terminal or state.final_response:
        return END
    if state.continue_model:
        return "model"
    if state.requested_tool:
        return "tools"
    return END


def _after_tools(state: ReactGraphState) -> str:
    """工具节点出口：进入 observe 节点，终态则直接结束。

    ``observe`` 节点独立承载「观察工具结果」步骤。终态或已产出最终回答时不再进 observe，
    避免对无观察价值的分支多做一次推理。

    参数:
        state: 当前 graph state。

    返回:
        ``"observe"`` 表示进入观察节点；``END`` 表示工作流结束。
    """

    if state.terminal or state.final_response:
        return END
    return "observe"


def _after_observe(state: ReactGraphState) -> str:
    """观察节点出口：回流模型继续推理，或结束工作流。

    参数:
        state: 当前 graph state（``observe`` 节点写回 ``terminal`` 与更新后的
            ``tool_error_count``）。

    返回:
        ``"model"`` 表示回到模型节点继续推理；``END`` 表示工作流结束（终态或已产出
        最终回答）。
    """

    if state.terminal or state.final_response:
        return END
    return "model"
