"""Delegated child execution result value object."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DelegationResult:
    """Represent the terminal result of a delegated child Agent run."""

    status: str
    child_turn_id: str
    summary: str | None = None
    error: str | None = None
