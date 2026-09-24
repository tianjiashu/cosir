"""子 Agent 工具的安全 UI 展示数据构造。"""

from typing import Any, Literal


ChildAgentOperation = Literal["send", "status"]


def build_child_agent_result_display_data(
    *,
    operation: ChildAgentOperation,
    child_task_id: int,
    child_run_id: int,
    status: str,
    agent_id: str | None,
    agent_name: str | None,
    final_output: str | None,
    end_reason: str | None,
) -> dict[str, Any]:
    """构造 ``child_agent_send/status`` 的 UI-only 结果。

    只保留 locator、生命周期和已持久化的最终摘要；不携带 prompt、异常堆栈或模型原始
    响应。该字典进入 Transport snapshot，但不进入模型上下文。
    """

    data: dict[str, Any] = {
        "kind": "child-agent-result",
        "operation": operation,
        "child_task_id": child_task_id,
        "child_run_id": child_run_id,
        "status": status,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "final_output": final_output,
        "end_reason": end_reason,
    }
    return data


def build_child_agent_wait_display_data(
    *,
    child_task_id: int,
    child_run_id: int,
    status: str,
    final_output: str | None,
    end_reason: str | None,
    timed_out: bool,
) -> dict[str, Any]:
    """构造单个 child-agent wait 的严格 Transport 展示数据。"""

    terminal = status if status in {"completed", "failed", "cancelled"} else "completed"
    if timed_out:
        return {
            "kind": "child-agent-wait-result",
            "timed_out": True,
            "messages": [],
            "pending": [{"child_task_id": child_task_id, "status": status}],
            "interrupted_by": None,
        }
    return {
        "kind": "child-agent-wait-result",
        "timed_out": False,
        "messages": [
            {
                "child_task_id": child_task_id,
                "child_run_id": child_run_id,
                "status": terminal,
                "final_output": final_output,
                "end_reason": end_reason,
            }
        ],
        "pending": [],
        "interrupted_by": None,
    }


__all__ = [
    "ChildAgentOperation",
    "build_child_agent_result_display_data",
    "build_child_agent_wait_display_data",
]
