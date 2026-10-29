"""交互终端工具族的共享辅助：取消检查、领域错误与 write / read 观察归一化为 ``ToolObservation``。

展示字段投影由 ``tools/display/terminal_display.py`` 负责；本模块不持有会话状态。
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.tools.display.terminal_display import build_terminal_observation_display_data
from app.tools.terminal_runtime.errors import (
    TerminalInputHeldByUserError,
    TerminalIntegrationRequiredError,
    TerminalSessionError,
)
from app.tools.terminal_runtime.observation_render import TerminalObservationResult

# 受控短提示（约 5 字）：只按错误类型映射，不透传异常正文。
_STATUS_HINTS: dict[type[TerminalSessionError], str] = {
    TerminalInputHeldByUserError: "用户输入中",
    TerminalIntegrationRequiredError: "缺少集成",
}


def cancelled(context: ToolExecutionContext) -> bool:
    """查询本工具所属 Task 是否收到停止信号。"""

    return cancellation_registry.is_cancelled(context.task_id)


def service_error_observation(tool_name: str, exc: TerminalSessionError) -> ToolObservation:
    """把终端领域错误转换为模型可消费的错误观察，并按 retryable 给出重试建议。"""

    return tool_error(
        tool_name,
        f"terminal operation failed ({exc.code}): {exc}. "
        + (
            "Retry after the transient condition clears."
            if exc.retryable
            else "Fix the session or arguments before retrying."
        ),
        status_hint=_STATUS_HINTS.get(type(exc), "终端失败"),
    )


def observation_result(tool_name: str, result: TerminalObservationResult) -> ToolObservation:
    """write / read 的观察文本原样交给模型；后台等待与交接声明挂起，后台占位标记 ``deferred``。"""

    observation = tool_success(
        tool_name=tool_name,
        content=result.text,
        display_data=build_terminal_observation_display_data(result),
        suspend=result.suspended,
    )
    observation.deferred = result.deferred
    return observation


def with_terminal_errors(
    tool_name: str,
    action: Callable[[], ToolObservation],
) -> ToolObservation:
    """领域错误归一化为错误观察；其他异常继续上抛，避免把缺陷伪装成工具失败。"""

    try:
        return action()
    except TerminalSessionError as exc:
        return service_error_observation(tool_name, exc)
