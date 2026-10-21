"""``terminal_start`` handler：创建 Agent 驱动的交互终端 session。

PTY 创建、shell 解析、cwd 边界校验与终端元数据登记都在 ``TerminalSessionService``；本模块
只做参数透传、取消前置检查与结果归一化。
"""

from typing import ClassVar

from app.core.tools.display.terminal_display import build_terminal_session_display_data
from app.core.tools.schemas import (
    TOOL_TERMINAL_START,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.core.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalStartArgs
from app.service import depends as service_depends


class TerminalStartTool(HandlerBase):
    """创建 Agent 驱动的持久 PTY session，并返回其元数据。

    职责：把 shell 与 cwd 交给 service 创建会话，返回可供 ``terminal_write`` /
    ``terminal_read`` / ``terminal_signal`` / ``terminal_close`` 使用的 ``session_id``。

    不负责：不写入输入、不读取输出、不关闭会话；前端不会调用本 handler，只能 attach 本工具
    返回的只读预览。

    风险级别为 ``high``：会话一旦建立，就是一个仍然存活的本地 shell。
    """

    name = TOOL_TERMINAL_START
    description = (
        "Start a task-owned interactive shell. Use terminal_list first to reuse existing sessions. "
        "Sessions survive completed, failed, or cancelled runs until terminal_close, shell exit, "
        "task deletion, or backend shutdown. There is no idle timeout. "
        "The frontend only previews its output; use terminal_write/read/signal/close "
        "to operate the session."
    )
    args_model = TerminalStartArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        shell: str = "auto",
        cwd: str | None = None,
        label: str | None = None,
        integration: str = "auto",
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """创建交互终端 session 并返回其元数据。

        参数:
            shell: 目标 shell；``auto`` 由 ``ShellResolver`` 按宿主平台解析。
            cwd: 会话初始目录，相对工作区根；None 表示工作区根。
            label: 可选用途标签，供后续 Run 通过 terminal_list 发现会话。
            execution_context: 执行上下文；提供会话归属（task / workspace）与取消查询边界。

        返回:
            成功时为携带 session 元数据的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            在本机创建 PTY 与 shell 子进程（不打开可见终端窗口），并把会话登记到 session
            service；不向前端发送任何输入。
        """

        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            service = service_depends.get_terminal_session_service()
            payload = service.start(
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                workspace_root=str(execution_context.workspace_root),
                shell=shell,
                integration=integration,
                cwd=cwd,
                label=label,
                run_id=execution_context.run_id,
            )
            return success_observation(
                self.name,
                payload,
                summary="Terminal session started.",
                display_payload=build_terminal_session_display_data(
                    payload,
                    include_terminal_info=True,
                ),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_start`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，
            ``parameters_schema`` 用宿主专属 shell 与 cwd 说明覆盖，权限 ``shell``、
            ``execution_mode="thread"``。

        异常:
            无。

        副作用:
            无；每次调用重新构造定义，不写注册表、不创建会话。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=describe_terminal_tool(self.name, self.description),
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=build_terminal_session_parameters_schema(self.name, self.args_model),
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="启动终端",
                icon="terminal",
                variant="terminal-session-start",
                surface="standalone",
                expandable=True,
                expand_layout="terminal",
                show_result=False,
            ),
        )
