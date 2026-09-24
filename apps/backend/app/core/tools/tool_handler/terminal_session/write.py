"""``terminal_write`` handler."""

from typing import ClassVar

from app.config.logging.logger import log
from app.core.tools.schemas import (
    TOOL_TERMINAL_WRITE,
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
from app.core.tools.tool_models import TerminalWriteArgs


def encode_terminal_input(data: str, *, submit: bool) -> bytes:
    """Encode terminal text and optionally append an explicit Enter byte.

    ``data`` is intentionally encoded as-is: literal ``\\r``, ``\\n`` and HTML entities
    are not decoded at this boundary. ``submit`` is the explicit protocol operation for
    appending one CR byte, which keeps command submission reliable across model transports.
    """

    return data.encode("utf-8") + (b"\r" if submit else b"")


class TerminalWriteTool(HandlerBase):
    """按 operation_id 幂等向已存在的 session 写入 Agent input。"""

    name = TOOL_TERMINAL_WRITE
    description = (
        "Write raw UTF-8 input to an existing hidden local terminal session and optionally "
        "wait for output. Set submit=true to append one real Enter key (CR); do not encode "
        "Enter as literal \\r/\\n or HTML entities."
    )
    permission: ClassVar[str] = "shell"
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "high"

    def execute(
        self,
        session_id: str,
        operation_id: str,
        data: str,
        submit: bool = False,
        after_seq: int | None = None,
        wait_ms: int = 500,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """写入 UTF-8 input；submit 显式追加 CR；相同 operation_id 不重复写入。"""

        if execution_context is None:
            raise ValueError("terminal_write requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        suspicious_tokens = tuple(
            token
            for token, present in (
                ("html_carriage_return", "&#13;" in data),
                ("literal_carriage_return_escape", "\\r" in data),
                ("literal_line_feed_escape", "\\n" in data),
            )
            if present
        )
        if suspicious_tokens:
            log.warning(
                "terminal_write_suspicious_input_encoding",
                extra={
                    "msg": "Terminal input 可能使用了字面转义或 HTML 控制字符实体",
                    "data": {"session_id": session_id, "tokens": suspicious_tokens},
                },
            )

        input_bytes = encode_terminal_input(data, submit=submit)

        def action() -> ToolObservation:
            result = require_service(execution_context).write(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                operation_id=operation_id,
                data=input_bytes,
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
                summary="Terminal input accepted.",
                display_payload=build_session_display_payload(
                    {"session_id": session_id, **result.to_dict()},
                    extra={"submitted": submit},
                ),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回交互终端工具定义。"""

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
                verb="输入终端",
                icon="terminal",
                variant="terminal-session-write",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
