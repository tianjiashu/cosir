"""child_agent_wait 工具：在等待时间内轮询单个子任务的最终输出。"""

import json
import time
from typing import ClassVar

from app.config.constant import Constant
from app.core.tools.display.child_agent_display import build_child_agent_wait_display_data
from app.core.tools.schemas import (
    TOOL_CHILD_AGENT_WAIT,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.child_task import ChildAgentWaitArgs
from app.service.depends import get_conversation_run_state_service, get_task_service

# 轮询间隔：子 Agent 完成一轮通常以秒到分钟计，1 秒足以兼顾及时性与查询开销。
_POLL_INTERVAL_SECONDS = 1.0


class ChildAgentWaitTool(HandlerBase):
    """轮询单个子任务的当前 Run，直到拿到 ``final_output``、进入终态或超时。

    职责边界：本类只做「读 task → 读 current_run → 判 ``final_output``」这一条 canonical
    轮询，不认领、不启动、不改写任何 Run 事实；等待期间父 Run 被取消时立即让出。
    """

    name: str = TOOL_CHILD_AGENT_WAIT
    description: str = (
        "This tool waits for the conclusion of exactly one child agent. To wait for several "
        "child agents, call this tool once per child agent (batching the calls in one reply "
        "is fine). Child agents usually run for a long time, so do not set too short a "
        "timeout. A long timeout does not delay the result: as soon as the child agent "
        "produces its final output, the tool returns immediately instead of waiting for the "
        "timeout to elapse."
    )
    permission: ClassVar[str] = "child_agent_wait"
    args_model: type[ChildAgentWaitArgs] = ChildAgentWaitArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "low"

    def __init__(self) -> None:
        """构造等待工具，装配任务与 Run 状态查询服务。

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
        timeout_seconds: float = 60.0,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """阻塞轮询指定子任务的当前 Run，直到拿到最终输出或超时。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）；
                本工具一次只等待这一个子任务。
            timeout_seconds: 本次等待上限（秒），须大于 0 且不超过 290；到点即返回
                ``timed_out``，不视为失败。
            execution_context: 父工具执行边界，提供父 Run 取消信号。

        返回:
            拿到最终输出时返回包含 ``timed_out=False`` 与 ``final_output`` 的 JSON 文本；
            超时返回 ``timed_out=True`` 与当前状态；缺少执行上下文、参数非法、子任务不存在、
            尚无 Run、Run 终态却没有最终输出、或父 Run 已取消时返回对应观察（取消为
            ``cancelled``，其余为 ``error``）。

        异常:
            无。``ValidationError`` / ``KeyError`` 均在本方法内归一化为错误观察。

        副作用:
            在工具线程内按间隔重复只读查询 task 与 run 记录（不写库）；父 Run 取消时提前返回。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_wait requires an execution context.",
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
                    "use the child_task_id returned by delegate_task, or "
                    "delegate the subtask first and retry with the corrected child_task_id."
                ),
                permission=self.permission,
                retryable=True,
            )

        is_parent_cancelled = execution_context.runtime_dependencies.is_run_cancelled
        deadline = time.monotonic() + timeout_seconds
        while True:
            if is_parent_cancelled is not None and is_parent_cancelled(execution_context.run_id):
                return tool_cancelled(
                    tool_name=self.name,
                    permission=self.permission,
                )

            run = self._run_state_service.get_run(child_task.current_run_id)

            if run.final_output:
                return tool_success(
                    tool_name=self.name,
                    permission=self.permission,
                    content=json.dumps(
                        {
                            "child_task_id": child_task_id,
                            "child_run_id": run.id,
                            "timed_out": False,
                            "status": run.status,
                            "final_output": run.final_output,
                            "end_reason": run.end_reason,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    display_data=build_child_agent_wait_display_data(
                        child_task_id=child_task_id,
                        child_run_id=run.id,
                        status=run.status,
                        final_output=run.final_output,
                        end_reason=run.end_reason,
                        timed_out=False,
                    ),
                )
            if run.status in Constant.Run.TERMINAL_STATUSES:
                return tool_error(
                    self.name,
                    f"child run {run.id} finished without final output ({run.status})",
                    reason=(
                        "the child run reached a terminal state without producing output; "
                        "read it with child_agent_status or re-delegate instead of waiting."
                    ),
                    permission=self.permission,
                    retryable=False,
                )
            if time.monotonic() >= deadline:
                return tool_success(
                    tool_name=self.name,
                    permission=self.permission,
                    content=json.dumps(
                        {
                            "child_task_id": child_task_id,
                            "child_run_id": run.id,
                            "timed_out": True,
                            "status": run.status,
                            "final_output": None,
                            "end_reason": None,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    display_data=build_child_agent_wait_display_data(
                        child_task_id=child_task_id,
                        child_run_id=run.id,
                        status=run.status,
                        final_output=None,
                        end_reason=None,
                        timed_out=True,
                    ),
                )

            remaining = deadline - time.monotonic()
            time.sleep(min(_POLL_INTERVAL_SECONDS, max(remaining, 0.0)))

    def to_definition(self) -> ToolDefinition:
        """构建 child_agent_wait 工具的注册定义。

        参数:
            无。

        返回:
            线程直跑的 child_agent_wait 工具定义；工具超时高于参数上限，保证参数内的
            等待不会被工具层提前中断。

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
            parallel_mode="serial",
            display=ToolDisplayHints(
                verb="等待子 Agent",
                icon="clock",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_wait_definition() -> ToolDefinition:
    """构建 child_agent_wait 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 child_agent_wait 工具定义。

    异常:
        无。

    副作用:
        创建一个 ``ChildAgentWaitTool`` 实例（构造期取得两个 service 单例）。
    """

    return ChildAgentWaitTool().to_definition()
