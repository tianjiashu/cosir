"""``terminal_signal`` handler：向交互终端 session 发送 interrupt / EOF / suspend。

能力由会话 worker 声明，投递成功不代表命令已经返回。
"""

from typing import ClassVar

from app.tools.display.terminal_display import build_terminal_session_display_data
from app.core.tool.schemas import (
    TOOL_TERMINAL_SIGNAL,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    success_observation,
    with_terminal_errors,
)
from app.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_models import TerminalSignalArgs
from app.service import depends as service_depends


class TerminalSignalTool(HandlerBase):
    """向交互终端 session 发送平台无关的 interrupt / EOF / suspend 信号。

    职责：把信号名交给 service 映射为 worker 的具体操作并投递到目标 PTY。

    不负责：不写入输入、不创建或关闭会话；信号能否生效取决于当前 PTY worker 声明的能力。

    风险级别为 ``high``：信号会中断会话中正在运行的进程。
    """

    name = TOOL_TERMINAL_SIGNAL
    description = "Send an interrupt, EOF, or suspend signal to a local terminal session."
    args_model = TerminalSignalArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        signal: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """向指定 session 发送平台无关信号。

        参数:
            session_id: ``terminal_start`` 返回的会话标识。
            signal: 信号名（``interrupt`` / ``eof`` / ``suspend``），由 service 映射为
                worker 的具体操作。
            execution_context: 执行上下文；提供会话归属（task / workspace）与取消查询边界。

        返回:
            成功时为携带 session 快照的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            向本机 PTY 投递信号；worker 不支持该信号时由 service 返回领域错误，而不是静默
            忽略。
        """

        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            payload = service_depends.get_terminal_session_service().signal(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                signal_name=signal,
            )
            return success_observation(
                self.name,
                payload,
                summary="Terminal signal sent.",
                display_payload=build_terminal_session_display_data(
                    payload,
                    extra={"signal": signal},
                ),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_signal`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述与 ``parameters_schema`` 由 ``descriptions.py``
            按宿主平台补全（含该平台的信号能力说明），权限 ``shell``、
            ``execution_mode="thread"``。

        异常:
            无。

        副作用:
            无；每次调用重新构造定义，不写注册表、不读会话状态。
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
                verb="控制终端",
                icon="square-terminal",
                variant="terminal-session-signal",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
