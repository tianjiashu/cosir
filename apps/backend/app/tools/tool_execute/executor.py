"""Tool handler executor."""

from dataclasses import replace
import logging
import multiprocessing
import queue
import traceback
from typing import Any, Mapping

from app.tools.tool_execute.results import tool_error, tool_success
from app.tools.schemas import ToolDefinition, ToolObservation


class ToolExecutor:
    """Execute tool handlers and normalize expected failures."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the executor."""

        self._logger = logger

    def execute(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        tool_call_id: str = "",
    ) -> ToolObservation:
        """Execute one tool handler with a timeout."""

        result_queue = multiprocessing.Queue(maxsize=1)
        process = multiprocessing.Process(
            target=_execute_handler,
            args=(tool.handler, dict(arguments), result_queue),
            daemon=True,
        )
        try:
            process.start()
        except Exception as exc:
            return tool_error(
                tool.name,
                str(exc),
                reason="handler_start_failed",
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        process.join(tool.timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(1)
            result_queue.close()
            result_queue.join_thread()
            self._logger.warning(
                "tool_execution_timed_out",
                extra={
                    "message_text": "tool execution timed out",
                    "data": {"tool_name": tool.name, "timeout_seconds": tool.timeout_seconds},
                },
            )
            return tool_error(
                tool.name,
                f"tool timed out after {tool.timeout_seconds} seconds",
                reason="timeout",
                retryable=True,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        try:
            status, payload = result_queue.get(timeout=1)
        except queue.Empty:
            status, payload = (
                "error",
                {
                    "message": "tool process exited without a result",
                    "traceback": "",
                },
            )
        finally:
            result_queue.close()
            result_queue.join_thread()

        if status == "error":
            self._logger.error(
                "tool_handler_failed",
                extra={
                    "message_text": "tool handler failed",
                    "data": {"tool_name": tool.name, "traceback": payload.get("traceback", "")},
                },
            )
            return tool_error(
                tool.name,
                payload.get("message", "tool handler failed"),
                reason="handler_exception",
                retryable=False,
                permission=tool.permission,
                tool_call_id=tool_call_id,
            )

        result = payload

        if isinstance(result, ToolObservation):
            if tool_call_id and not result.tool_call_id:
                return replace(result, tool_call_id=tool_call_id)
            return result
        return tool_success(tool, str(result), tool_call_id=tool_call_id)


def _execute_handler(handler, arguments: dict[str, Any], result_queue) -> None:
    """Run a tool handler inside an isolated child process."""

    try:
        result_queue.put(("success", handler(**arguments)))
    except Exception as exc:  # noqa: BLE001 - serialized across process boundary.
        result_queue.put(
            (
                "error",
                {
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
        )
