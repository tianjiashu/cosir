"""Delegated child execution result value object."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationResult:
    """Represent the terminal result of a delegated child Agent run."""

    status: str
    child_run_id: int
    summary: str | None = None
    error: str | None = None
