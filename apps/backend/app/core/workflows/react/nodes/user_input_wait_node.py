"""通用用户输入等待节点：保留 LangGraph 断点并等待外部业务动作恢复。"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.core.workflows.react.worflow_state.state import ReactGraphState


def _wait_requests(state: ReactGraphState) -> list[dict[str, Any]]:
    """从最近工具结果中提取经过工具展示契约声明的等待请求标识。"""

    results = state.last_tool_results
    observations = results.get("observations")
    if not isinstance(observations, list):
        return []
    requests: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        display_data = observation.get("display_data")
        if not isinstance(display_data, dict) or display_data.get("requires_user_input") is not True:
            continue
        request = display_data.get("user_input_request")
        if not isinstance(request, dict):
            raise ValueError("需要用户输入的工具结果缺少 user_input_request")
        kind = request.get("kind")
        request_id = request.get("request_id")
        if not isinstance(kind, str) or not kind or not isinstance(request_id, (str, int)):
            raise ValueError("用户输入请求必须包含 kind 和 request_id")
        requests.append({"kind": kind, "request_id": str(request_id)})
    return requests


def user_input_wait_node(state: ReactGraphState) -> dict[str, object]:
    """在用户处理工具声明的输入请求前挂起图，并在外部恢复时继续后继节点。

    LangGraph checkpointer 持久化 interrupt 断点；Conversation Run 的 waiting 状态由执行器在
    确认断点并完成本地资源清理后写入。此节点不创建业务记录，也不执行任何工具或网络请求。

    参数:
        state: 当前 ReAct graph state，最近一批工具结果需声明用户输入请求。

    返回:
        用户恢复图后继续沿固定边进入后继节点时，本节点不额外修改 graph state。

    异常:
        ValueError: 工具声明了用户输入要求但未提供有效请求标识。
        LangGraph interrupt 控制流异常: 首次运行时挂起节点；调用方不得吞掉该控制流。

    副作用:
        通过 LangGraph checkpointer 写入工作流断点；不写业务数据库。
    """

    requests = _wait_requests(state)
    if not requests:
        raise ValueError("用户输入等待节点没有可等待的请求")
    interrupt({"kind": "user_input_required", "requests": requests})
    return {}
