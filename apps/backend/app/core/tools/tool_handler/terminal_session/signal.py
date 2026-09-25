"""``terminal_signal`` handler：向交互终端 session 发送 interrupt / EOF / suspend。

信号语义与可用性由 PTY worker 声明的能力决定；Windows worker 不支持 PTY signal，因此本工具
在 Windows 上不注册（见 ``TerminalSignalTool.avaliable()``）。
"""

import platform
from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_GROUP_TERMINAL_SESSION,
    TOOL_TERMINAL_SIGNAL,
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
from app.core.tools.tool_models import TerminalSignalArgs


class TerminalSignalTool(HandlerBase):
    """向交互终端 session 发送平台无关的 interrupt / EOF / suspend 信号。

    职责：把信号名交给 service 映射为 worker 的具体操作并投递到目标 PTY。

    不负责：不写入输入、不创建或关闭会话；信号能否生效取决于当前 PTY worker 声明的能力。

    风险级别为 ``high``：信号会中断会话中正在运行的进程。
    """

    name = TOOL_TERMINAL_SIGNAL
    description = "Send an interrupt, EOF, or suspend signal to a local terminal session."
    permission: ClassVar[str] = "shell"
    args_model = TerminalSignalArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "high"
    group = TOOL_GROUP_TERMINAL_SESSION


    def execute(
        self,
        session_id: str,
        signal: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """向指定 session 发送平台无关信号。

        参数:
            session_id: ``terminal_start`` 返回的会话标识。
            signal: 信号名（``interrupt`` / ``eof`` / ``suspend``），由 service 映射为
                worker 的具体操作。
            execution_context: 执行上下文；为 None 时直接报错，因为缺少 workspace、
                task/run 标识与取消查询边界。

        返回:
            成功时为携带 session 快照的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            向本机 PTY 投递信号；worker 不支持该信号时由 service 返回领域错误，而不是静默
            忽略。
        """

        if execution_context is None:
            raise ValueError("terminal_signal requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            payload = require_service(execution_context).signal(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                signal_name=signal,
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal signal sent.",
                display_payload=build_session_display_payload(payload, extra={"signal": signal}),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_signal`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述与 ``parameters_schema`` 由 ``descriptions.py``
            按宿主平台补全（含该平台的信号能力说明），权限 ``shell``、
            ``execution_mode="thread"``、``risk_level="high"``。

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
                verb="控制终端",
                icon="square-terminal",
                variant="terminal-session-signal",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )

    def avaliable(self) -> bool:
        """判断当前平台是否注册 ``terminal_signal``。

        Windows worker 目前只提供 ConPTY 的读写与关闭能力，不声明 ``signal_interrupt``、
        ``signal_eof_canonical`` 或 ``signal_suspend``；因此必须在工具系统装配期隐藏
        ``terminal_signal``，避免模型收到一个注定返回 unsupported 的工具定义。Unix worker
        保持现有能力。

        返回:
            非 Windows 平台返回 True（注册该工具）；Windows 返回 False，
            ``build_terminal_signal_definition()`` 据此返回 ``None``。

        异常:
            无。

        副作用:
            无；只读取 ``platform.system()``。
        """

        return platform.system() != "Windows"
