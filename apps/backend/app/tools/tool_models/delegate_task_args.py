"""Pydantic arguments for the delegate_task tool."""

from pydantic import BaseModel


class DelegateTaskArgs(BaseModel):
    """Describe a child-agent task requested by a parent agent."""

    child_agent_id: str
    delegation_type: str
    prompt: str
    requested_tools: list[str]
