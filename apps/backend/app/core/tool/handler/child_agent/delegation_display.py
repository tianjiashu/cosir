"""delegate_task 的 UI 展示数据构造。"""

from typing import Any



def build_delegation_display_data(
    *,
    title: str,
    child_agent_id: str,
    delegation_id: int | None = None,
    child_task_id: int | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """构造父级委派展示数据，不携带 prompt 或 child 正文。

    子 Agent 的最终输出属于 child task 的结果，不属于父级委派卡片；子 Task 的运行状态也不在这里：
    展示数据不是事实源，前端按 ``child_task_id`` 读应用级任务状态推送。
    """

    data: dict[str, Any] = {
        "kind": "delegation-result",
        "title": title,
        "child_agent_id": child_agent_id,
    }
    if role:
        data["role"] = role
    if delegation_id is not None:
        data["delegation_id"] = delegation_id
    if child_task_id is not None:
        data["child_task_id"] = child_task_id
    return data
