"""child_agent_status 工具：读取子任务当前 Run 的状态与最终输出。"""

import json
from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_CHILD_AGENT_STATUS,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.child_task import ChildAgentStatusArgs
from app.service.depends import get_conversation_run_state_service, get_task_service


class ChildAgentStatusTool(HandlerBase):
    """按子任务的 ``current_run_id`` 读取其当前 Run 状态。

    职责边界：本类只做「task → current_run_id → run」这一条 canonical 读取，不推断生命周期、
    不写入任何事实；父任务归属由调用方（模型）通过 ``child_task_id`` 指定。
    """

    name: str = TOOL_CHILD_AGENT_STATUS
    description: str = (
        "Read the current status and final output of a delegated child task. "
        "Uses the child task's current run; call it after delegate_task when you need "
        "to decide whether the child is still running or already finished."
    )
    permission: ClassVar[str] = "child_agent_status"
    args_model: type[ChildAgentStatusArgs] = ChildAgentStatusArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "low"

    def __init__(self) -> None:
        """构造状态读取工具，装配任务与 Run 状态查询服务。

        参数:
            无。

        返回:
            无（构造函数）。

        异常:
            无（服务取得失败由 ``get_*`` 自身语义决定）。

        副作用:
            从依赖装配取得 ``TaskService`` 与 ``ConversationRunStateService`` 单例引用。
        """

        self._task_service = get_task_service()
        self._run_state_service = get_conversation_run_state_service()

    def execute(
        self,
        child_task_id: int,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """读取指定子任务当前 Run 的状态投影。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）。
            execution_context: 父工具执行边界；仅用于确认调用发生在某次 Run 内。

        返回:
            成功时返回包含 ``child_task_id`` / ``child_run_id`` / ``status`` /
            ``final_output`` / ``end_reason`` / ``agent_id`` 的 JSON 文本；缺少执行上下文、
            参数非法、子任务不存在或尚无 Run 时返回错误观察。

        异常:
            无。``ValidationError`` 与 ``KeyError`` 均在本方法内归一化为错误观察。

        副作用:
            各一次只读查询：读取 task 记录与其当前 Run 记录；不写任何事实。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_status requires an execution context.",
                reason="Call this tool from inside a running turn instead of directly.",
                permission=self.permission,
            )

        try:
            child_task = self._task_service.get_task(child_task_id)
        except KeyError:
            return tool_error(
                self.name,
                f"child task not found: {child_task_id}",
                reason=(
                    "verify child_task_id against the value returned by "
                    "delegate_task; this task does not exist."
                ),
                permission=self.permission,
                retryable=False,
            )

        if not child_task.is_child or child_task.parent_task_id != execution_context.task_id:
            return tool_error(
                tool_name=self.name,
                error="child_task_not_delegated_by_caller",
                reason=(
                    "child_task_id does not refer to a child task delegated by this task; "
                    "use the child_task_id returned by delegate_task, or "
                    "delegate the subtask first and retry with the corrected child_task_id."
                ),
                permission=self.permission,
                retryable=True,
            )
        run_id = child_task.current_run_id

        try:
            run = self._run_state_service.get_run(run_id)
        except KeyError:
            return tool_error(
                self.name,
                f"child run not found: {run_id}",
                reason=(
                    "the child task points at a run that no longer exists; re-delegate the "
                    "work instead of retrying this read."
                ),
                permission=self.permission,
                retryable=False,
            )

        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {
                    "child_task_id": child_task_id,
                    "child_run_id": run.id,
                    "status": run.status,
                    "final_output": run.final_output,
                    "end_reason": run.end_reason,
                    "agent_id": run.agent_id,
                    "agent_name": child_task.title,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """构建 child_agent_status 工具的注册定义。

        参数:
            无。

        返回:
            线程直跑的 child_agent_status 工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="读取子 Agent 状态",
                icon="info",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_status_definition() -> ToolDefinition:
    """构建 child_agent_status 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 child_agent_status 工具定义。

    异常:
        无。

    副作用:
        创建一个 ``ChildAgentStatusTool`` 实例（构造期取得两个 service 单例）。
    """

    return ChildAgentStatusTool().to_definition()
