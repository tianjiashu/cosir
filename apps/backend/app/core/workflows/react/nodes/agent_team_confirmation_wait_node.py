"""Agent Team 确认等待节点：在用户确认 Team 前挂起主 Agent。"""

from __future__ import annotations

from langgraph.types import interrupt


def agent_team_confirmation_wait_node(_state: object) -> dict[str, object]:
    """在用户确认 Team 前挂起主 Agent，并在恢复后回到模型节点。

    Agent Team 预览已经作为工具结果写入主 Agent context 后，观察节点会先把主 Run
    标记为 ``cancelled``，再路由到本节点。这里使用 LangGraph interrupt 保留图断点；
    用户确认并且 Team 进入终态后，协调器通过既有 resume 入口恢复该 Run，模型节点即可
    读取协调器注入的 TeamResult。

    参数:
        _state: 当前 ReAct graph state。该节点不读取业务字段，断点由 LangGraph 保存。

    返回:
        空 state 增量；恢复后由固定图边继续进入 ``model``。

    异常:
        LangGraph 初次执行时会通过 ``interrupt`` 挂起当前节点；调用方不应吞掉该控制流。

    副作用:
        不创建数据库记录；只创建 LangGraph 断点并暂停当前 Conversation Run 的工作流。
    """

    interrupt({"reason": "agent_team_waiting_confirmation"})
    return {}
