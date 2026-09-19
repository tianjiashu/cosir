"""Task ChangeSet Keep/Revert request body."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskChangeActionRequest(BaseModel):
    """Address one or more immutable task ChangeSet group ids."""

    model_config = ConfigDict(extra="forbid")

    change_ids: list[str] = Field(min_length=1)

    @field_validator("change_ids")
    @classmethod
    def require_nonempty_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("change_ids must not contain empty ids")
        return values
