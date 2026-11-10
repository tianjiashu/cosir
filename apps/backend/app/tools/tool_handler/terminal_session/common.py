"""交互终端工具族的共享辅助：取消检查、领域错误与 read / write 观察归一化为
``ToolObservation``。

展示字段投影由 ``tools/display/terminal_display.py`` 负责；本模块不持有会话状态。
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.tools.display.terminal_display import build_terminal_observation_display_data
from app.tools.terminal_runtime.errors import (
    TerminalIntegrationRequiredError,
    TerminalPrefillRejectedError,
    TerminalSessionError,
)
from app.tools.terminal_runtime.render import TerminalObservationResult

# 受控短提示（约 5 字）：只按错误类型映射，不透传异常正文；未登记的类型回落为「终端失败」。
_STATUS_HINTS: dict[type, str] = {
    TerminalPrefillRejectedError: "无法填入",
    TerminalIntegrationRequiredError: "缺少集成",
}


def cancelled(context: ToolExecutionContext) -> bool:
    """本 Task 收到停止信号，或用户单独取消了这次调用。

    条件等待在工具线程里持锁等待，只靠这里复查取消：单次调用的取消也要能打断它，不能只认 Task 级
    信号（thread 工具的执行器只在 handler 执行前检查一次）。
    """

    return should_stop_call(context.task_id, context.tool_call_id)


def observation_result(tool_name: str, result: TerminalObservationResult) -> ToolObservation:
    """read / write 的观察文本原样交给模型，展示数据投影动作行。"""

    return tool_success(
        tool_name=tool_name,
        content=result.text,
        display_data=build_terminal_observation_display_data(result),
    )


def with_terminal_errors(
    tool_name: str,
    action: Callable[[], ToolObservation],
) -> ToolObservation:
    """领域错误归一化为错误观察（事实来自 message，下一步动作来自错误类型的 advice）；其他异常继续
    上抛，避免把缺陷伪装成工具失败。"""

    try:
        return action()
    except TerminalSessionError as exc:
        content = f"terminal operation failed ({exc.code}): {exc}"
        if exc.advice:
            content = f"{content} {exc.advice}"
        return tool_error(tool_name, content, status_hint=_STATUS_HINTS.get(type(exc), "终端失败"))
