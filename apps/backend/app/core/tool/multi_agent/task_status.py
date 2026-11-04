"""task_status 工具：读取自己派生的 Task（委派子 Agent、Agent fork）当前 Run 的状态与最终输出。"""

import json
from typing import Any, ClassVar

from app.core.tool.multi_agent.args.task_status_args import TaskStatusArgs
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TASK_STATUS,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.service.depends import get_conversation_run_ledger, get_task_service

class TaskStatusTool(HandlerBase):
    """只做「task → current_run_id → run」这一条读取，不推断生命周期、不写任何事实。

    只能读自己派生的 Task：上级的状态对下级没有用，它要的东西经 ``send_message`` 问。
    """

    name: str = TOOL_TASK_STATUS
    description: str = (
        "Read the current status and final output of a task you started (a delegated child "
        "agent or a fork). Call it when you need to decide whether it is still running or "
        "already finished, instead of waiting."
    )
    args_model: type[TaskStatusArgs] = TaskStatusArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        self._task_service = get_task_service()
        self._ledger = get_conversation_run_ledger()

    @staticmethod
    def _agent_task_display_data(
        *,
        task_id: int,
        agent_name: str | None,
        final_output: str | None,
        end_reason: str | None,
    ) -> dict[str, Any]:
        """构造状态查询的 UI-only 结果。

        只保留对方 Task 的定位（按 Task 寻址，不含 run_id）、名字与已持久化的最终摘要；不携带 prompt、
        异常堆栈或模型原始响应，也不镜像对方 Run 的状态（前端读应用级任务状态推送）。
        """

        return {
            "kind": "agent-task-result",
            "operation": "status",
            "task_id": task_id,
            "agent_name": agent_name,
            "final_output": final_output,
            "end_reason": end_reason,
        }

    def execute(self, task_id: int, execution_context: ToolExecutionContext) -> ToolObservation:
        """返回对方最新 Run 的状态、最终输出与结束原因（不回传 run_id）；寻址不对时返回错误观察。"""
        try:
            task = self._task_service.get_task(task_id)
        except KeyError:
            return tool_error(
                self.name,
                f"task not found: {task_id}. Use the child_task_id from delegate_task or the "
                "fork_task_id from fork_task.",
            )
        if not task.derived_by(execution_context.task_id):
            return tool_error(
                self.name,
                "task_id does not refer to a task you started; use the child_task_id returned by "
                "delegate_task or the fork_task_id returned by fork_task.",
            )
        if task.current_run_id is None:
            # 刚建出的 Task 要等它的 actor 开出第一个 Run，这段时间没有 Run 可读。
            return tool_error(
                self.name,
                f"task {task_id} has not started its first run yet; check again later or use "
                "wait_messages for its conclusion.",
            )
        run = self._ledger.get_run(task.current_run_id)
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "task_id": task_id,
                    "name": task.title,
                    "status": run.status,
                    "final_output": run.final_output,
                    "end_reason": run.end_reason,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=self._agent_task_display_data(
                task_id=task_id,
                agent_name=task.title,
                final_output=run.final_output,
                end_reason=run.end_reason,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
        )

def build_task_status_definition() -> ToolDefinition:
    """供工具注册表登记的 task_status 定义。"""
    return TaskStatusTool().to_definition()
