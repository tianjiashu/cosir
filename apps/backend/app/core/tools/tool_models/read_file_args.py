"""Pydantic arguments for read_file."""

from pydantic import BaseModel, ConfigDict, Field


class ReadFileArgs(BaseModel):
    """Validated arguments accepted by the read_file tool."""

    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(description="Path relative to the project root.")
    offset: int = Field(default=1, ge=1, description="1-indexed line number to start reading from.")
    limit: int = Field(default=500, ge=1, le=2000, description="Maximum number of lines to return.")
