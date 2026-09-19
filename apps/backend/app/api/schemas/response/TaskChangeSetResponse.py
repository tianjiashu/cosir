"""Task ChangeSet final-net-diff API response models."""

from pydantic import BaseModel


class TaskChangeNetDiffResponse(BaseModel):
    state: str
    additions: int
    deletions: int
    patch: str | None
    truncated: bool
    has_unrendered_changes: bool


class TaskFileChangeResponse(BaseModel):
    change_id: str
    paths: list[str]
    action: str
    status: str
    last_run_id: int | None
    operation_count: int
    net_diff: TaskChangeNetDiffResponse | None


class TaskChangeSetResponse(BaseModel):
    task_id: int
    files: list[TaskFileChangeResponse]


class TaskChangeResultResponse(BaseModel):
    change_id: str
    outcome: str
    reason_code: str | None = None
    message: str | None = None


class TaskChangeActionResponse(BaseModel):
    task_id: int
    results: list[TaskChangeResultResponse]
    change_set: TaskChangeSetResponse
