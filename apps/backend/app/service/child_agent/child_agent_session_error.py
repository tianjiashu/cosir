"""Stable domain errors for Child Agent session operations."""

from __future__ import annotations


class ChildAgentSessionError(RuntimeError):
    """Stable model-facing Child Agent session error."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)
