"""``terminal_close`` handler：关闭 Agent 创建的交互终端 session。

关闭语义由 ``TerminalSessionService`` 裁决：结束 shell 进程树并释放 PTY。本模块只做参数
透传、取消前置检查与结果归一化。
"""

from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_GROUP_TERMINAL_SESSION,
    TOOL_TERMINAL_CLOSE,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_handler.terminal_session.common import (
    build_session_display_payload,
    cancelled,
    cancelled_observation,
    require_service,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalCloseArgs


class TerminalCloseTool(HandlerBase):
    """关闭 Agent 创建的交互终端 session，并结束其 shell 进程树。

    职责：把 ``session_id`` 交给 service 关闭，并把领域错误与取消归一化为工具观察。

    不负责：不创建 session、不写入输入、不发送信号（分别见 ``terminal_start`` /
    ``terminal_write`` / ``terminal_signal``）。

    风险级别为 ``high``：关闭不可逆，会终止该会话中仍在运行的进程。
    """

    name = TOOL_TERMINAL_CLOSE
    description = "Close an existing local terminal session and its shell process tree."
    permission: ClassVar[str] = "shell"
    args_model = TerminalCloseArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "high"
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """显式关闭指定 session，并返回关闭后的 session 快照。

        参数:
            session_id: ``terminal_start`` 返回的会话标识。
            execution_context: 执行上下文；为 None 时直接报错，因为缺少 workspace、
                task/run 标识与取消查询边界。

        返回:
            成功时为携带终态 session 快照的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            向本机 PTY 发出关闭并回收 shell 进程树；前端预览 WebSocket 断开**不**触发本
            操作，会话也不会因预览退出而关闭。
        """

        if execution_context is None:
            raise ValueError("terminal_close requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            payload = require_service(execution_context).close(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal session closed.",
                display_payload=build_session_display_payload(payload),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_close`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，权限
            ``shell``、``execution_mode="thread"``、``risk_level="high"``。

        异常:
            无。

        副作用:
            无；每次调用重新构造定义，不写注册表、不读会话状态。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=describe_terminal_tool(self.name, self.description),
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=build_terminal_session_parameters_schema(
                self.name, self.args_model
            ),
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("shell",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="关闭终端",
                icon="x",
                variant="terminal-session-close",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
