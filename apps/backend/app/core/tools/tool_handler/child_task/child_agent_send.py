"""child_agent_send 工具：给已有子任务追加输入并启动新的 Run。"""

import asyncio
import json
from typing import ClassVar

from app.config.constant import Constant
from app.core.tools.display.child_agent_display import build_child_agent_result_display_data
from app.config.logging.logger import log
from app.core.tools.schemas import (
    TOOL_CHILD_AGENT_SEND,
    TOOL_GROUP_CHILD_AGENT,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.child_task.child_agent_create import CHILD_BANNED_TOOLS
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.child_task import ChildAgentSendArgs
from app.models import ConversationRunCommand
from app.service.depends import (
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_task_service,
)

# 只等 executor 完成「登记 + 建后台 task」这一段，不等子 Agent 跑完。
_START_ACK_TIMEOUT_SECONDS = 10.0


class ChildAgentSendTool(HandlerBase):
    """给已有子任务追加一条输入，并为它建立一个新 Run 后交给执行器后台运行。

    职责边界：本类不直接执行 Agent，只做「解析 child 运行参数 → 建 Run → 认领 → 交
    ``ConversationRunExecutor`` 后台执行」这条编排；也不持有进程内会话，重复调用即追加
    新轮次。子 Agent 的运行终态由它自己的 workflow 落定。
    """

    name: str = TOOL_CHILD_AGENT_SEND
    description: str = (
        "Send a message to a child agent after confirming that it has already produced a "
        "conclusion in its child task, reusing that child task. Note: if the child agent has "
        "not reached a terminal conclusion yet, this tool fails."
    )
    permission: ClassVar[str] = "child_agent_send"
    args_model: type[ChildAgentSendArgs] = ChildAgentSendArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "medium"
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        """构造发送工具，装配任务、Run 与执行器服务。

        参数:
            无。

        返回:
            无（构造函数）。

        异常:
            无。

        副作用:
            从依赖装配取得 ``TaskService`` / ``ConversationRunService`` /
            ``ConversationRunStateService`` / ``ConversationRunExecutor`` 单例引用。
        """

        self._task_service = get_task_service()
        self._run_service = get_conversation_run_service()
        self._run_state_service = get_conversation_run_state_service()
        self._run_executor = get_conversation_run_executor()

    def execute(
        self,
        child_task_id: int,
        message: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """为指定子任务创建一个新 Run 并启动后台执行。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）。
            message: 追加给子 Agent 的自由文本输入；原样作为新 Run 的输入。
            child_agent_id: 可选的 child Agent 标识；缺省沿用该子任务最近一个 Run 使用的
                agent。子任务尚无任何 Run 且此处为空时返回错误观察。
            execution_context: 父工具执行边界，提供事件循环与父 Run 事实。

        返回:
            成功时返回包含 ``child_task_id`` / ``child_run_id`` / ``status`` 的 JSON 文本；
            缺少执行上下文或运行期事件循环、参数非法、子任务不存在、无法确定 child Agent、
            建 Run 或启动失败时返回错误观察。

        异常:
            无。``ValidationError`` / ``KeyError`` / 执行器启动异常均在本方法内归一化为
            错误观察。

        副作用:
            在目标子任务下创建一个 ``pending`` Run、认领为 ``running``，并在父 Run 的事件
            循环上登记一次后台执行；不等待子 Agent 执行结束。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_send requires an execution context.",
                reason="Call this tool from inside a running turn instead of directly.",
                permission=self.permission,
            )
        loop = execution_context.runtime_dependencies.runtime_event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "child_agent_send_runtime_unavailable",
                reason=(
                    "the parent runtime event loop is unavailable, so the child run cannot "
                    "be started; retry from a normal turn."
                ),
                permission=self.permission,
                retryable=False,
            )

        try:
            # 存在性校验：子任务不存在时直接返回确定性错误，不再继续创建 Run。
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
                retryable=True,
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

        current_run_id = child_task.current_run_id
        current_run = self._run_state_service.get_run(current_run_id)
        if current_run.status not in Constant.Run.TERMINAL_STATUSES:
            return tool_error(
                tool_name=self.name,
                error="error",
                reason=(
                    "the child agent is still running; inspect it with child_agent_status or "
                    "wait for its conclusion with child_agent_wait, and call this tool again "
                    "only after it reaches a terminal state."
                ),
            )

        try:
            child_run = self._run_service.create_run(
                task_id=child_task_id,
                agent_id=current_run.agent_id,
                provider_id=current_run.provider_id,
                model_name=current_run.model_name,
                reasoning_effort=current_run.reasoning_effort,
                run_command=ConversationRunCommand(display_text=message),
            )
            claimed = self._run_state_service.claim_pending_run(child_run.id)
            if claimed is None:
                return tool_error(
                    self.name,
                    "child_agent_send_run_not_claimable",
                    reason=(
                        "the new child run is not claimable (already terminal or claimed); "
                        "read its status before deciding what to do next."
                    ),
                    permission=self.permission,
                    retryable=False,
                )
            # 执行器入口是协程：在父 Run 的事件循环上调度，并只等待登记完成。
            future = asyncio.run_coroutine_threadsafe(
                self._run_executor.start(child_run.id, "fresh", ban_tools=list(CHILD_BANNED_TOOLS)),
                loop,
            )
            future.result(timeout=_START_ACK_TIMEOUT_SECONDS)
        except Exception as exc:
            log.exception(
                "child_agent_send_failed",
                extra={
                    "msg": "为子任务创建 Run 或启动后台执行失败",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_task_id": child_task_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                self.name,
                "child_agent_send_failed",
                reason=(
                    "the follow-up run could not be started; inspect the backend log and "
                    "retry later."
                ),
                permission=self.permission,
                retryable=True,
            )

        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {
                    "child_task_id": child_task_id,
                    "child_run_id": child_run.id,
                    "child_agent_id": current_run.agent_id,
                    "agent_name": child_task.title,
                    "status": "running",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=build_child_agent_result_display_data(
                operation="send",
                child_task_id=child_task_id,
                child_run_id=child_run.id,
                status="running",
                agent_id=current_run.agent_id,
                agent_name=child_task.title,
                final_output=None,
                end_reason=None,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """构建 child_agent_send 工具的注册定义。

        参数:
            无。

        返回:
            线程直跑的 child_agent_send 工具定义；工具观察只确认新 Run 已登记，
            不等待子 Agent 执行结束。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="发送给子 Agent",
                icon="send",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_child_agent_send_definition() -> ToolDefinition:
    """构建 child_agent_send 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 child_agent_send 工具定义。

    异常:
        无。

    副作用:
        创建一个 ``ChildAgentSendTool`` 实例（构造期取得四个 service 单例）。
    """

    return ChildAgentSendTool().to_definition()
