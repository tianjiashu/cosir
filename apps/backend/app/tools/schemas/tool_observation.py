"""Normalized tool execution result."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolObservation:
    """A tool_execute-facing observation produced by a tool call."""

    tool_name: str
    status: str
    content: str
    error: str = ""
    reason: str = ""
    retryable: bool = False
    permission: str = ""
    tool_call_id: str = ""
    artifact_id: str = ""
