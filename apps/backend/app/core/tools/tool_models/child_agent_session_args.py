"""Argument models for Child Agent session interaction tools."""

from typing import Literal

from pydantic import BaseModel, Field


class ChildAgentWaitTarget(BaseModel):
    """One direct Child Task and its at-least-once Run cursor."""

    child_task_id: int = Field(gt=0)
    after_run_id: int | None = Field(default=None, gt=0)


class ChildAgentWaitArgs(BaseModel):
    """Validated model arguments for ``child_agent_wait``."""

    targets: list[ChildAgentWaitTarget] | None = None
    wait_mode: Literal["any", "all"] = "any"
    timeout_seconds: float = Field(default=30.0, ge=0, le=300)


class ChildAgentSendArgs(BaseModel):
    """Validated arguments for an in-process Child Agent mailbox message."""

    child_task_id: int = Field(gt=0)
    message: str = Field(min_length=1)
    message_id: str = Field(min_length=1, max_length=128)


class ChildAgentStatusArgs(BaseModel):
    """Validated arguments for a canonical Child Agent status read."""

    child_task_id: int = Field(gt=0)


class ChildAgentCloseArgs(BaseModel):
    """Validated arguments for an idempotent Child Agent close."""

    child_task_id: int = Field(gt=0)


class ChildAgentTerminalMessage(BaseModel):
    """Canonical terminal record exposed to the parent model."""

    child_task_id: int
    child_run_id: int
    status: Literal["completed", "failed", "cancelled"]
    final_output: str | None = None
    end_reason: str | None = None


class ChildAgentPendingState(BaseModel):
    """Stable pending target projection returned by a partial wait."""

    child_task_id: int
    status: str | None = None


class ChildAgentWaitResult(BaseModel):
    """Fixed result shape for timeout, completion, and interruption paths."""

    timed_out: bool
    messages: list[ChildAgentTerminalMessage] = Field(default_factory=list)
    pending: list[ChildAgentPendingState] = Field(default_factory=list)
    interrupted_by: Literal[None, "parent_cancelled", "session_closed", "shutdown"] = None
