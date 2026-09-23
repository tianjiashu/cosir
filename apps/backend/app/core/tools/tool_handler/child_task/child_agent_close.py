"""child_agent_close 工具：向子任务当前 Run 发送取消信号。"""

import json
from typing import ClassVar

from pydantic import ValidationError

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.child_task import ChildAgentCloseArgs
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.service.depends import get_conversation_run_state_service, get_task_service

# Run 的终态集合：命中后无需再发取消信号，直接按事实回告。
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {
        ConversationRunStatus.COMPLETED.value,
        ConversationRunStatus.FAILED.value,
        ConversationRunStatus.CANCELLED.value,
    }
)


class ChildAgentCloseTool(HandlerBase):
    """向子任务当前 Run 写入进程内取消信号。

    职责边界：本类只「发信号」——在 ``cancellation_registry`` 标记该 Run，使其 workflow 在
    下一个检查点协作收束；不落库、不直接取消 asyncio task、不代替 workflow 落定终态。终态
    结果仍需用 ``child_agent_status`` 或 ``child_agent_wait`` 观察。
    """

    name: str = "child_agent_close"
    description: str = (
        "Request cancellation of the current run of a delegated child task. "
        "It only signals cancellation: the child's own workflow converges the run, "
        "so confirm the terminal state afterwards with child_agent_status or "
        "child_agent_wait."
    )
    permission: ClassVar[str] = "child_agent_close"
    args_model: type[ChildAgentCloseArgs] = ChildAgentCloseArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "medium"

    def __init__(self) -> None:
        """构造关闭工具，装配任务与 Run 状态查询服务。

        参数:
            无。

        返回:
            无（构造函数）。

        异常:
            无。

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
        """对指定子任务的当前 Run 发送取消信号。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）。
            execution_context: 父工具执行边界；仅用于确认调用发生在某次 Run 内。

        返回:
            成功时返回包含 ``child_task_id`` / ``child_run_id`` / ``cancel_requested`` /
            ``status`` 的 JSON 文本；缺少执行上下文、参数非法、子任务不存在或尚无 Run 时
            返回错误观察。

        异常:
            无。``ValidationError`` 与 ``KeyError`` 均在本方法内归一化为错误观察。

        副作用:
            子任务当前 Run 仍处于 active 时，向其写入进程内取消信号（不落库）；幂等，
            重复调用对同一 Run 不产生额外效果。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_close requires an execution context.",
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
                    "verify child_task_id against the value returned by delegate_task; "
                    "this task does not exist."
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
                    "use the child_task_id returned by delegate_task_for_sub_agent, or "
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
                    "the child task points at a run that no longer exists; there is nothing "
                    "to cancel."
                ),
                permission=self.permission,
                retryable=False,
            )

        if run.status in _TERMINAL_RUN_STATUSES:
            # 已是终态：不写信号，直接按事实回告，保持「取消请求是幂等」的语义。
            return tool_success(
                tool_name=self.name,
                permission=self.permission,
                content=json.dumps(
                    {
                        "child_task_id": child_task_id,
                        "child_run_id": run.id,
                        "cancel_requested": False,
                        "status": run.status,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

        cancellation_registry.mark_cancelled(run.id)
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {
                    "child_task_id": child_task_id,
                    "child_run_id": run.id,
                    "cancel_requested": True,
                    "status": run.status,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    def to_definition(self) -> ToolDefinition:
        """构建 child_agent_close 工具的注册定义。

        参数:
            无。

        返回:
            同步执行（``handler_kind="sync"``）、线程直跑的 child_agent_close 工具定义。

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
                verb="关闭子 Agent",
                icon="stop",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_close_definition() -> ToolDefinition:
    """构建 child_agent_close 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 child_agent_close 工具定义。

    异常:
        无。

    副作用:
        创建一个 ``ChildAgentCloseTool`` 实例（构造期取得两个 service 单例）。
    """

    return ChildAgentCloseTool().to_definition()
