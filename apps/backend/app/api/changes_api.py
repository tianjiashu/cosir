"""Task-level ChangeSet query and baseline Keep/Revert HTTP boundary."""

from fastapi import HTTPException

from app.api.schemas.request.TaskChangeActionRequest import TaskChangeActionRequest
from app.api.schemas.response.TaskChangeSetResponse import (
    TaskChangeActionResponse,
    TaskChangeSetResponse,
)
from app.app import app
from app.service.depends import get_task_change_set_service


@app.get("/tasks/{task_id}/changes", response_model=TaskChangeSetResponse)
async def get_changes(task_id: int) -> TaskChangeSetResponse:
    """Return the pending task ChangeSet with final diffs from current baselines."""

    try:
        return TaskChangeSetResponse.model_validate(
            get_task_change_set_service().get_changes(task_id)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/changes/keep", response_model=TaskChangeActionResponse)
async def keep_changes(
    task_id: int,
    request: TaskChangeActionRequest,
) -> TaskChangeActionResponse:
    """Set each selected group's current workspace state as its next baseline."""

    return _apply_changes(task_id, request, "keep")


@app.post("/tasks/{task_id}/changes/revert", response_model=TaskChangeActionResponse)
async def revert_changes(
    task_id: int,
    request: TaskChangeActionRequest,
) -> TaskChangeActionResponse:
    """Revert each selected group as a unit to its latest Keep/Revert baseline."""

    return _apply_changes(task_id, request, "revert")


def _apply_changes(
    task_id: int,
    request: TaskChangeActionRequest,
    action: str,
) -> TaskChangeActionResponse:
    try:
        response = get_task_change_set_service().apply(
            task_id,
            action,
            request.change_ids,
        )
        results = response.get("results", [])
        if results and all(item.get("reason_code") == "operation_busy" for item in results):
            raise HTTPException(
                status_code=409,
                detail="task operation is busy; retry after the current Run finishes",
            )
        return TaskChangeActionResponse.model_validate(response)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task or workspace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
