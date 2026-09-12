"""Hidden terminal session tool shared helpers."""

import json
from collections.abc import Callable

from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.service.terminal.errors import TerminalSessionError
from app.service.terminal.terminal_session_service import TerminalSessionService


def require_service(context: ToolExecutionContext) -> TerminalSessionService:
    """取得仅供同进程 terminal handler 使用的 session service。"""

    service = context.runtime_dependencies.terminal_session_service
    if service is None:
        raise TerminalSessionError("terminal session service is not available")
    return service


def cancelled(context: ToolExecutionContext) -> bool:
    """查询当前 Agent Run 是否已取消。"""

    callback = context.runtime_dependencies.is_run_cancelled
    return callback is not None and context.run_id > 0 and callback(context.run_id)


def cancelled_observation(tool_name: str, permission: str) -> ToolObservation:
    """构造 terminal handler 的取消观察。"""

    return tool_cancelled(
        tool_name,
        permission=permission,
    )


def service_error_observation(
    tool_name: str,
    permission: str,
    exc: TerminalSessionError,
) -> ToolObservation:
    """把领域错误转换为模型可消费的工具错误。"""

    return tool_error(
        tool_name,
        f"terminal operation failed: {exc}",
        reason=(
            f"the terminal service rejected this operation with {exc.code}. "
            + (
                "Retry after the transient condition clears."
                if exc.retryable
                else "Fix the session or arguments before retrying."
            )
        ),
        retryable=exc.retryable,
        permission=permission,
        status_hint="终端失败",
    )


def success_observation(
    tool_name: str,
    permission: str,
    payload: dict[str, object],
    *,
    summary: str,
) -> ToolObservation:
    """构造终端工具成功观察；完整 output 只进入结构化 payload。"""

    return tool_success(
        tool_name=tool_name,
        permission=permission,
        content=f"{summary}\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}",
        display_data={"kind": "terminal-session", **payload},
    )


def with_terminal_errors(
    tool_name: str,
    permission: str,
    action: Callable[[], ToolObservation],
) -> ToolObservation:
    """归一化 terminal session 领域错误，不吞掉非领域编程错误。"""

    try:
        return action()
    except TerminalSessionError as exc:
        return service_error_observation(tool_name, permission, exc)
