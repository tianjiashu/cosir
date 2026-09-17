"""delegate_task 的 UI 展示数据构造。"""

from typing import Any


def build_delegation_display_data(
    *,
    title: str,
    child_agent_id: str,
    delegation_id: int | None = None,
    child_task_id: int | None = None,
    child_run_id: int | None = None,
    status: str,
    role: str | None = None,
) -> dict[str, Any]:
    """构造父级委派结果的最小展示数据，不携带 prompt 或 child 正文。"""

    data: dict[str, Any] = {
        "kind": "delegation-result",
        "title": title,
        "child_agent_id": child_agent_id,
        "status": status,
    }
    if role:
        data["role"] = role
    if delegation_id is not None:
        data["delegation_id"] = delegation_id
    if child_task_id is not None:
        data["child_task_id"] = child_task_id
    if child_run_id is not None:
        data["child_run_id"] = child_run_id
    return data
