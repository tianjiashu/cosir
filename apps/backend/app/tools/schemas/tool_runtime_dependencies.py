"""Runtime dependencies available to tool handlers."""

from dataclasses import dataclass

from app.tools.schemas.delegate_task_executor import DelegateTaskExecutor


@dataclass(frozen=True)
class ToolRuntimeDependencies:
    """Hold optional runtime capabilities supplied to tool execution contexts."""

    delegate_task_executor: DelegateTaskExecutor | None = None
