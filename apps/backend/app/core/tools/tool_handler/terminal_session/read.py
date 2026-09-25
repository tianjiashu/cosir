"""``terminal_read`` handler：按输出游标非破坏性读取交互终端 session 的输出。

读取不消费其他订阅者或前端预览的数据；输出缓冲、游标语义与保留窗口都由
``TerminalSessionService`` 裁决，本模块只做参数透传、取消前置检查与结果归一化。
"""

from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_TERMINAL_READ,
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
from app.core.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    """按输出游标非破坏性读取交互终端 session 的输出。

    职责：把 ``after_seq`` 游标与等待时长交给 service，取回该窗口内的新输出。

    不负责：不写入输入、不改变 session 状态；输出保留窗口与裁剪策略归
    ``TerminalSessionService``。

    风险级别为 ``medium``：操作本身只读，但会把终端输出带入模型上下文。
    """

    name = TOOL_TERMINAL_READ
    description = (
        "Read new output from a local terminal session using an output sequence cursor. "
        "When the command is expected to run for a long time, pass a suitable wait_ms to "
        "wait for its output instead of polling with frequent reads."
    )
    permission: ClassVar[str] = "shell"
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "medium"

    def execute(
        self,
        session_id: str,
        after_seq: int | None = None,
        wait_ms: int = 1000,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """读取 session 中游标之后的新输出。

        参数:
            session_id: ``terminal_start`` 返回的会话标识。
            after_seq: 只返回序列号大于该值的输出；None 表示由 service 决定默认窗口。
            wait_ms: 等待新输出的最长时间，单位毫秒；0 表示立即返回。
            execution_context: 执行上下文；为 None 时直接报错，因为缺少 workspace、
                task/run 标识与取消查询边界。

        返回:
            成功时为携带 session 快照与新输出的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            只读取本机 PTY 的输出缓冲，不消费数据：其他订阅者与前端预览仍能读到同一段输出。
        """

        if execution_context is None:
            raise ValueError("terminal_read requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            result = require_service(execution_context).read(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                after_seq=after_seq,
                wait_ms=wait_ms,
                is_cancelled=lambda: cancelled(execution_context),
            )
            if cancelled(execution_context):
                return cancelled_observation(self.name, self.permission)
            return success_observation(
                self.name,
                self.permission,
                {"session_id": session_id, **result.to_dict()},
                summary="Terminal output read.",
                display_payload=build_session_display_payload(
                    {"session_id": session_id, **result.to_dict()},
                ),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_read`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，权限
            ``shell``、``execution_mode="thread"``、``risk_level="medium"``。

        异常:
            无。

        副作用:
            无；每次调用重新构造定义，不写注册表、不读会话状态。
        """

        return ToolDefinition(
            name=self.name,
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
                verb="读取终端",
                icon="terminal",
                variant="terminal-session-read",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
