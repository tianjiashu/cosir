"""``terminal_write`` handler：向已存在的交互终端 session 写入 Agent 输入。

本模块只负责输入编码、幂等键透传与结果归一化；PTY 生命周期、写入门闸、输出缓冲和
幂等裁决都在 ``TerminalSessionService``。模型可见描述由 ``descriptions.py`` 按当前
宿主平台补全。
"""

from typing import ClassVar

from app.config.logging.logger import log
from app.core.tools.schemas import (
    TOOL_GROUP_TERMINAL_SESSION,
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
    """把终端文本编码为写入字节，并可选追加一个显式回车字节。

    参数:
        data: 原样写入的文本；本边界**不做**解码，字面 ``\\r``、``\\n`` 与 HTML 实体都按
            普通字符写入。
        submit: 为 True 时在末尾追加一个真实 CR 字节（``0x0D``）。

    返回:
        UTF-8 编码后的字节串；``submit`` 为 True 时以单个 CR 结尾。

    异常:
        无；``data`` 的类型由调用方的参数模型提前校验。

    副作用:
        无；纯函数，不接触终端会话、注册表或日志。
    """

    return data.encode("utf-8") + (b"\r" if submit else b"")


class TerminalWriteTool(HandlerBase):
    """向已存在的交互终端 session 写入 Agent 输入，并按 operation_id 幂等。

    职责：编码本次输入（``submit`` 追加一个真实 CR）、把相同 ``operation_id`` 的重复调用
    交给 service 判重、把领域错误与取消归一化为工具观察。

    不负责：不创建或关闭 session（分别见 ``terminal_start`` / ``terminal_close``）、不做
    PTY 写入裁决与输出缓冲（归 ``TerminalSessionService``）、不决定模型可见描述（归
    ``descriptions.py``）。

    风险级别为 ``high``：输入会被送进仍然存活的 shell，语义等同于在该终端手动敲键。
    """

    name = TOOL_TERMINAL_WRITE
    description = (
        "Write raw UTF-8 input to an existing hidden local terminal session and optionally "
        "wait for output. Set submit=true to append one real Enter key (CR); do not encode "
        "Enter as literal \\r/\\n or HTML entities. When the process inside the terminal has "
        "exited, use this tool to write again and reuse the same session for the next task."
    )
    permission: ClassVar[str] = "shell"
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "high"
    group = TOOL_GROUP_TERMINAL_SESSION

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
        """向 session 写入 UTF-8 输入，并返回本次操作后的输出窗口。

        参数:
            session_id: ``terminal_start`` 返回的会话标识。
            operation_id: 本次写入的幂等键；相同键的重复调用不会重复写入。
            data: 原样写入的文本；字面 ``\\r``、``\\n`` 与 HTML 实体在本边界不解码。
            submit: 为 True 时追加一个真实 CR 字节，用于可靠提交命令。
            after_seq: 只返回序列号大于该值的输出；None 表示由 service 决定默认窗口。
            wait_ms: 等待新输出的最长时间，单位毫秒。
            execution_context: 执行上下文；为 None 时直接报错，因为缺少 workspace、
                task/run 标识与取消查询边界。

        返回:
            成功时为携带 session 快照的成功观察；run 已取消时为取消观察；领域错误经
            :func:`with_terminal_errors` 归一化为错误观察。

        异常:
            ValueError: ``execution_context`` 缺失时抛出，属调用方编程错误。

        副作用:
            把编码后的字节写入本机 PTY，并可能等待新输出；输入疑似使用字面转义或 HTML
            控制字符实体时写 ``terminal_write_suspicious_input_encoding`` 警告日志（只记
            session_id 与命中的标记名，不记输入内容）。
        """

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
        """返回可注册到工具注册表的 ``terminal_write`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，
            ``parameters_schema`` 用宿主专属 shell 说明覆盖，权限 ``shell``、
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
                verb="输入终端",
                icon="terminal",
                variant="terminal-session-write",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
