"""工具调用宿主方法的唯一入口，以及链路层错误到工具观察的转换。

工具只在工作线程里经 :func:`call_host` 调用宿主：投递、等待、取消与时限都在这里收口，工具只处理
结果与它自己能力的错误码。
"""

from __future__ import annotations

from typing import Any

from app.core.tool.execute.thread_wait import ToolCallStopped, wait_for_call
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.service import depends as service_depends
from app.tools.host.errors import CANCELLED, TIMEOUT, UNAVAILABLE, HostCallError


def call_host(
    method: str,
    params: dict[str, Any],
    *,
    execution_context: ToolExecutionContext | None,
    timeout_seconds: float,
) -> Any:
    """调用宿主方法并返回其结果；只能在工具线程调用。

    超时与取消也以 :class:`HostCallError`（``TIMEOUT`` / ``CANCELLED``）上浮，工具只需一个
    ``except`` 分支：自己能力的错误码自己解释，其余交给 :func:`host_error_observation`。

    异常:
        HostCallError: 链路层或能力层失败。
    """

    meta: dict[str, Any] = {}
    if execution_context is not None:
        meta = {
            "task_id": execution_context.task_id,
            "tool_call_id": execution_context.tool_call_id,
        }
    future = service_depends.get_host_link().submit(method, params, meta)
    try:
        return wait_for_call(future, execution_context, timeout_seconds)
    except ToolCallStopped as exc:
        raise HostCallError(CANCELLED, "the tool call was cancelled") from exc
    except TimeoutError as exc:
        raise HostCallError(TIMEOUT, f"no response within {timeout_seconds:.0f} seconds") from exc


def host_error_observation(
    tool_name: str, error: HostCallError, tool_call_id: str = ""
) -> ToolObservation:
    """把链路层失败转成工具观察；工具未解释的能力层错误码按通用失败处理。"""

    if error.code == CANCELLED:
        return tool_cancelled(tool_name, tool_call_id)
    if error.code == UNAVAILABLE:
        return tool_error(
            tool_name,
            "the desktop host is not available (the backend is not running inside the desktop app, "
            "or the app is reconnecting). This capability cannot be used right now; do not retry "
            "in a loop.",
            tool_call_id,
            status_hint="宿主不可用",
        )
    if error.code == TIMEOUT:
        return tool_error(
            tool_name,
            f"the desktop host did not respond in time ({error.message}); the action may still "
            "have taken effect. Check the current state before retrying.",
            tool_call_id,
            status_hint="宿主超时",
        )
    return tool_error(
        tool_name,
        f"the desktop host failed to perform the action ({error.code}: {error.message}).",
        tool_call_id,
    )
