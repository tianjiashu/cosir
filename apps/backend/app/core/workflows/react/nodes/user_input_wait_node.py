"""通用用户输入等待节点：保留 LangGraph 断点并等待外部业务动作恢复。"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from app.core.workflows.react.node_helper.common import _runtime_config
from app.core.workflows.react.worflow_state.state import ReactGraphState


def _wait_requests(state: ReactGraphState) -> list[dict[str, Any]]:
    """从最近工具结果中提取经过工具展示契约声明的等待请求标识。

    返回结构:
        一个列表，每个元素为 ``{"kind": str, "request_id": str}`` 字典：

        - ``kind``: 等待请求的种类，来自工具结果
          ``display_data.user_input_request.kind`` 的字符串值。
        - ``request_id``: 等待请求的标识，取自
          ``display_data.user_input_request.request_id``，统一规整为字符串。

        列表元素与 ``state.last_tool_results.observations`` 中声明了
        ``requires_user_input`` 的工具结果一一对应（顺序一致）。无匹配项时返回空列表。
    """

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

    首次进入时先通过操作门面将 Conversation Run 迁移为 ``waiting_for_input``，再由
    LangGraph checkpointer 持久化 interrupt 断点。恢复该节点的 interrupt 时 LangGraph 会从
    节点开头重放；此时只消费恢复值，不重复迁移状态。续跑后首次到达等待节点仍会迁移状态。
    调用方在执行器收尾前不得恢复该 Run。此节点不创建业务记录，也不执行任何工具或网络请求。

    参数:
        state: 当前 ReAct graph state，最近一批工具结果需声明用户输入请求。

    返回:
        用户恢复图后继续沿固定边进入后继节点时，本节点不额外修改 graph state。

    异常:
        ValueError: 工具声明了用户输入要求但未提供有效请求标识。
        LangGraph interrupt 控制流异常: 首次运行时挂起节点；调用方不得吞掉该控制流。

    副作用:
        更新 Conversation Run 状态并发布状态事件；通过 LangGraph checkpointer 写入工作流断点。
    """

    requests = _wait_requests(state)
    if not requests:
        raise ValueError("用户输入等待节点没有可等待的请求")
    runtime_config = _runtime_config()
    # LangGraph 恢复 interrupt 时会重放整个节点；该路径不能再次迁移回 waiting。
    if not runtime_config.resuming_user_input_wait:
        runtime_config.operations.mark_waiting_for_input_if_running()

    #interrupt(...) 里的字典是 LangGraph 中断载荷（interrupt payload）
    # 核心作用：把"为什么挂起 + 需要什么输入"带到图外面
    # LangGraph 的 interrupt(payload) 会暂停图执行，并把 payload 通过 checkpointer
    # 暴露给外部驱动方（conversation_run_executor.py / transport 层）。外部在调用
    # graph.invoke(...) 拿到结果时，会从 __interrupt__ 里取出这个字典的 .value。
    # 所以这个字典是对外通信的契约，不是内部 state。
    interrupt({"kind": "user_input_required", "requests": requests})
    return {}
