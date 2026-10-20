"""交互终端工具族的共享辅助函数。

这里集中 terminal handler 共用的四类小能力：取进程内 session service、统一的取消检查、
把领域错误与成功结果归一化为 ``ToolObservation``。展示字段投影由
``core/tools/display/terminal_display.py`` 负责；本模块不持有会话状态，也不实现 PTY 能力。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.service.terminal.errors import TerminalRepeatedReadError, TerminalSessionError

if TYPE_CHECKING:
    from app.service.terminal.terminal_session_service import TerminalSessionService


def require_service(context: ToolExecutionContext) -> TerminalSessionService:
    """取得仅供同进程 terminal handler 使用的 session service。

    参数:
        context: 本次工具调用的执行上下文，其 ``runtime_dependencies`` 携带注入的 service。

    返回:
        当前进程的 ``TerminalSessionService``。

    异常:
        TerminalSessionError: 运行期依赖未注入该 service 时抛出，属装配缺失而非调用方参数
            错误。

    副作用:
        无；只读取执行上下文上的依赖引用。
    """

    service = context.runtime_dependencies.terminal_session_service
    if service is None:
        raise TerminalSessionError("terminal session service is not available")
    return service


def cancelled(context: ToolExecutionContext) -> bool:
    """查询本工具所属 Task 是否收到停止信号；无 run 绑定的直接调用视为不可取消。"""

    return context.run_id > 0 and cancellation_registry.is_cancelled(context.task_id)


def cancelled_observation(tool_name: str) -> ToolObservation:
    """构造 terminal handler 的取消观察。

    参数:
        tool_name: 工具名，写入观察的工具标识。

    返回:
        ``status="cancelled"`` 的 ``ToolObservation``；用户取消与执行失败是两种不同语义，
        因此不复用错误观察。

    异常:
        无。

    副作用:
        无。
    """

    return tool_cancelled(
        tool_name,
    )


def service_error_observation(
    tool_name: str,
    exc: TerminalSessionError,
) -> ToolObservation:
    """把终端领域错误转换为模型可消费的工具错误观察。

    参数:
        tool_name: 工具名，写入观察的工具标识。
        exc: service 抛出的 ``TerminalSessionError``。

    返回:
        重复读取单独解释为观察限制，UI 显示「重复读取」，不暗示命令失败或延长等待必然恢复。
        其他领域错误继续按 retryable 提供重试建议，UI 显示「终端失败」。

    异常:
        无；``exc`` 本身已是归一化后的领域错误。

    副作用:
        无；只构造观察对象，不写日志、不改会话状态。
    """

    if isinstance(exc, TerminalRepeatedReadError):
        return tool_error(
            tool_name,
            f"{exc.code}: {exc}",
            reason=(
                "Increasing wait_ms does not reset the counter or guarantee recovery. "
                "Normal reads resume when new output or state changes are observed. "
                "Inspect prior prompts before deciding whether to wait longer or provide input."
            ),
            retryable=exc.retryable,
            status_hint="重复读取",
        )
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
        status_hint="终端失败",
    )


def agent_session_payload(payload: dict[str, object]) -> dict[str, object]:
    """只移除单个会话结果的预览水位；列表调用方逐项投影，不修改原快照或观察事件。"""
    return {
        key: value
        for key, value in payload.items()
        if key not in {"first_available_seq", "next_seq"}
    }


def success_observation(
    tool_name: str,
    payload: dict[str, object],
    *,
    summary: str,
    display_payload: dict[str, object],
) -> ToolObservation:
    """构造终端工具的成功观察。

    参数:
        tool_name: 工具名，写入观察的工具标识。
        payload: service 结构化结果；以紧凑 JSON 追加到 ``content`` 供模型读取。
            顶层预览水位在此剔除；sessions 列表由调用方逐项使用 agent_session_payload。
        summary: 一行英文摘要，作为 ``content`` 首行。
        display_payload: 已按 allowlist 投影的展示数据，写入 ``display_data``。

    返回:
        ``status="success"`` 的 ``ToolObservation``：``content`` 为摘要加紧凑 JSON，
        ``display_data`` 使用调用方从 display 模块得到的投影结果。

    异常:
        无。

    副作用:
        无；只构造观察对象。
    """

    agent_payload = agent_session_payload(payload)
    encoded = json.dumps(agent_payload, ensure_ascii=False, separators=(",", ":"))
    return tool_success(
        tool_name=tool_name,
        content=f"{summary}\n{encoded}",
        display_data=display_payload,
    )


def with_terminal_errors(
    tool_name: str,
    action: Callable[[], ToolObservation],
) -> ToolObservation:
    """执行一次终端操作，并把领域错误归一化为错误观察。

    参数:
        tool_name: 工具名，写入观察的工具标识。
        action: 无参可调用对象，内部完成 service 调用与成功观察构造。

    返回:
        ``action`` 正常返回时原样透传其结果；抛出 ``TerminalSessionError`` 时返回
        :func:`service_error_observation` 构造的错误观察。

    异常:
        不捕获其他异常：``ValueError`` 等编程错误继续向上抛出，避免把缺陷伪装成工具失败。

    副作用:
        ``action`` 自身的副作用（PTY 读写、关闭会话等）原样发生。
    """

    try:
        return action()
    except TerminalSessionError as exc:
        return service_error_observation(tool_name, exc)
