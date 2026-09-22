"""Async ``child_agent_wait`` tool and its explicit canonical-read boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Protocol

from pydantic import ValidationError

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_models.child_agent_session_args import (
    ChildAgentWaitArgs,
    ChildAgentWaitResult,
)
from app.service.child_agent.async_child_agent_wait_coordinator import (
    AsyncChildAgentWaitCoordinator,
    WaiterCapacityExceeded,
    WaitQueryResult,
)
from app.service.child_agent.child_agent_session_error import ChildAgentSessionError


class ChildAgentWaitReader(Protocol):
    """Short synchronous canonical Run/SQLite query used by the wait tool."""

    def __call__(
        self,
        *,
        parent_task_id: int,
        parent_run_id: int,
        targets: list[dict[str, int | None]] | None,
        wait_mode: str,
    ) -> WaitQueryResult | ChildAgentWaitResult | Mapping[str, Any]:
        """Return a query wrapper or an explicitly convertible canonical result."""
        ...


def _coerce_wait_result(value: object) -> ChildAgentWaitResult:
    """Convert the reader's supported result shapes into the canonical Pydantic model."""

    if isinstance(value, ChildAgentWaitResult):
        return value
    if isinstance(value, Mapping):
        try:
            payload = dict(value)
            # The reader's short-query mapping may omit the derived timeout flag;
            # the coordinator supplies that flag from its own terminal outcome.
            payload.setdefault("timed_out", False)
            return ChildAgentWaitResult.model_validate(payload)
        except ValidationError as exc:
            raise TypeError("reader Mapping is not a valid ChildAgentWaitResult") from exc
    raise TypeError(
        "child_agent_wait reader must return ChildAgentWaitResult, a Mapping, "
        "or WaitQueryResult containing one of those values"
    )


def _coerce_query_result(value: object) -> WaitQueryResult:
    """Convert one reader result while preserving the coordinator's readiness predicate."""

    if isinstance(value, WaitQueryResult):
        if value.value is None:
            return value
        return WaitQueryResult(ready=value.ready, value=_coerce_wait_result(value.value))
    if isinstance(value, ChildAgentWaitResult | Mapping):
        return WaitQueryResult(ready=True, value=_coerce_wait_result(value))
    raise TypeError(
        "child_agent_wait reader must return ChildAgentWaitResult, a Mapping, "
        "or WaitQueryResult containing one of those values"
    )


class ChildAgentWaitTool:
    """Await Child Agent terminal records through injected runtime services.

    The reader owns parent/child ownership checks and canonical Run ordering.  This
    handler only validates model arguments, delegates the blocking-free wait, and
    serializes the fixed final-output-shaped result.
    """

    name: str = "child_agent_wait"
    description: str = (
        "Wait for terminal messages from one or more direct Child Agents. "
        "Completed messages include the canonical final output."
    )
    permission: ClassVar[str] = "child_agent_wait"
    args_model: type[ChildAgentWaitArgs] = ChildAgentWaitArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "low"

    def __init__(
        self,
        reader: ChildAgentWaitReader | Callable[..., object] | None = None,
        coordinator: AsyncChildAgentWaitCoordinator | None = None,
    ) -> None:
        self._reader = reader
        self._coordinator = coordinator

    async def execute_async(
        self,
        *,
        targets: list[dict[str, Any]] | None = None,
        wait_mode: str = "any",
        timeout_seconds: float = 30.0,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """Wait asynchronously and return canonical terminal message records."""

        if execution_context is None:
            return tool_error(
                self.name,
                "child_agent_wait requires an execution context.",
                reason="Provide the parent Run execution context before waiting.",
                permission=self.permission,
            )
        parsed = ChildAgentWaitArgs(
            targets=targets,
            wait_mode=wait_mode,
            timeout_seconds=timeout_seconds,
        )
        reader = self._reader or execution_context.runtime_dependencies.child_agent_wait_reader
        coordinator = (
            self._coordinator or execution_context.runtime_dependencies.child_agent_wait_coordinator
        )
        if reader is None or coordinator is None:
            return tool_error(
                self.name,
                "child_agent_wait is not configured.",
                reason=(
                    "Configure the canonical Child Agent wait reader and coordinator "
                    "before waiting."
                ),
                permission=self.permission,
            )

        serialized_targets = (
            [target.model_dump() for target in parsed.targets]
            if parsed.targets is not None
            else None
        )
        if serialized_targets is None:
            snapshotter = execution_context.runtime_dependencies.child_agent_wait_target_snapshot
            if snapshotter is not None:
                serialized_targets = snapshotter(
                    parent_task_id=execution_context.task_id,
                    parent_run_id=execution_context.run_id,
                )

        def canonical_query() -> WaitQueryResult:
            raw_result = reader(
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
                targets=serialized_targets,
                wait_mode=parsed.wait_mode,
            )
            return _coerce_query_result(raw_result)

        try:
            outcome = await coordinator.wait_async(
                execution_context.run_id,
                canonical_query,
                timeout_seconds=parsed.timeout_seconds,
            )
        except WaiterCapacityExceeded:
            return tool_error(
                self.name,
                "child_agent_wait_concurrency_exceeded",
                reason=(
                    "the bounded Child Agent waiter capacity is currently full; "
                    "wait for an existing waiter to finish before retrying."
                ),
                permission=self.permission,
            )
        except ChildAgentSessionError as exc:
            return tool_error(
                self.name,
                exc.code,
                reason=f"Child Agent wait request was rejected: {exc.code}.",
                permission=self.permission,
            )
        except (TypeError, ValidationError) as exc:
            return tool_error(
                self.name,
                f"invalid child_agent_wait reader result: {exc}",
                reason=(
                    "the canonical Child Agent wait reader returned a value outside its "
                    "ChildAgentWaitResult/Mapping contract; fix the reader contract before "
                    "retrying."
                ),
                permission=self.permission,
            )
        try:
            if outcome.value is None:
                if not outcome.timed_out and outcome.interrupted_by is None:
                    raise TypeError(
                        "canonical wait query completed without a ChildAgentWaitResult"
                    )
                result = ChildAgentWaitResult(
                    timed_out=outcome.timed_out,
                    interrupted_by=outcome.interrupted_by,
                )
            else:
                result = _coerce_wait_result(outcome.value)
        except (TypeError, ValidationError) as exc:
            return tool_error(
                self.name,
                f"invalid child_agent_wait reader result: {exc}",
                reason=(
                    "the canonical Child Agent wait reader returned a value outside its "
                    "ChildAgentWaitResult/Mapping contract; fix the reader contract before "
                    "retrying."
                ),
                permission=self.permission,
            )

        result = result.model_copy(
            update={
                "timed_out": outcome.timed_out or result.timed_out,
                "interrupted_by": outcome.interrupted_by or result.interrupted_by,
            }
        )
        payload = result.model_dump(mode="json")
        result = {
            "timed_out": payload["timed_out"],
            "messages": list(payload.get("messages", [])),
            "pending": list(payload.get("pending", [])),
            "interrupted_by": payload.get("interrupted_by"),
        }
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            display_data={"kind": "child-agent-wait-result", **payload},
        )

    def to_definition(self) -> ToolDefinition:
        """Return the serial async-only tool definition."""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute_async,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            handler_kind="async",
            parallel_mode="serial",
            display=ToolDisplayHints(
                verb="等待子 Agent",
                icon="clock",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=True,
            ),
        )


def build_child_agent_wait_definition() -> ToolDefinition:
    """Build the runtime-injected ``child_agent_wait`` definition."""

    return ChildAgentWaitTool().to_definition()
