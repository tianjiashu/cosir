"""Assistant Transport command for selecting tools unavailable in one Run."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class BanToolsPayload(BaseModel):
    """Validated tool-name selection carried by ``BanToolsCommand``.

    ``ban_tools`` may be empty to preserve the default where every tool remains available.
    Names must be unique, nonblank strings; availability against the current main-agent catalog
    is checked by the transport service after request parsing.

    Raises:
        ValidationError: When a name is blank, duplicated, or not a string.

    Side effects:
        None. This model only validates and stores the request payload.
    """

    model_config = ConfigDict(extra="forbid")

    ban_tools: list[StrictStr]

    @field_validator("ban_tools")
    @classmethod
    def validate_tool_names(cls, names: list[str]) -> list[str]:
        """Reject blank or duplicate names while allowing an empty selection.

        Args:
            names: Strict string tool names parsed from the request.

        Returns:
            The unchanged validated list.

        Raises:
            ValueError: If a name is blank or appears more than once. Pydantic reports this as
                a request validation error.

        Side effects:
            None.
        """

        if any(not name.strip() for name in names):
            raise ValueError("ban_tools entries must not be blank")
        if len(names) != len(set(names)):
            raise ValueError("ban_tools entries must be unique")
        return names


class BanToolsCommand(BaseModel):
    """Assistant UI custom command that configures disabled tools for one Run.

    This schema preserves the wire shape ``type="custom"``, ``name="ban-tools"`` and a typed
    ``payload``. It validates command identity and payload shape only; the transport service
    validates tool names against the current main-agent catalog and persists them in Run Extra.

    Raises:
        ValidationError: When the wire discriminator, command name, command ID, or payload is
            malformed.

    Side effects:
        None. Persistence and catalog lookup belong to the transport service.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["custom"]
    commandId: str = Field(min_length=1, max_length=128)
    name: Literal["ban-tools"]
    payload: BanToolsPayload
