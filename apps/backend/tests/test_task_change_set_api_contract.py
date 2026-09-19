"""Task ChangeSet API request/response wire contract tests."""

import pytest
from pydantic import ValidationError

from app.api.schemas.request.TaskChangeActionRequest import TaskChangeActionRequest
from app.api.schemas.response.TaskChangeSetResponse import (
    TaskChangeActionResponse,
    TaskChangeSetResponse,
)


def test_change_action_uses_change_ids_for_one_id_or_a_batch() -> None:
    assert TaskChangeActionRequest(change_ids=["chg_one"]).change_ids == ["chg_one"]
    assert TaskChangeActionRequest(change_ids=["chg_one", "chg_two"]).change_ids == [
        "chg_one",
        "chg_two",
    ]

    with pytest.raises(ValidationError):
        TaskChangeActionRequest()
    with pytest.raises(ValidationError):
        TaskChangeActionRequest(change_ids=[])
    with pytest.raises(ValidationError):
        TaskChangeActionRequest(change_ids=["  "])
    with pytest.raises(ValidationError):
        TaskChangeActionRequest(change_id="chg_one")


def test_action_response_embeds_the_refreshed_task_projection() -> None:
    projection = {
        "task_id": 7,
        "files": [
            {
                "change_id": "chg_one",
                "paths": ["src/app.ts"],
                "action": "modified",
                "status": "pending",
                "last_run_id": 31,
                "operation_count": 2,
                "net_diff": {
                    "state": "verified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "diff --git a/src/app.ts b/src/app.ts",
                    "truncated": False,
                    "has_unrendered_changes": False,
                },
            }
        ],
    }
    parsed = TaskChangeSetResponse.model_validate(projection)
    assert parsed.files[0].operation_count == 2

    action = TaskChangeActionResponse.model_validate(
        {
            "task_id": 7,
            "results": [{"change_id": "chg_one", "outcome": "reverted"}],
            "change_set": projection,
        }
    )
    assert action.change_set.files[0].net_diff.state == "verified"
