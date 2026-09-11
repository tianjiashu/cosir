"""Load persisted LangChain messages for an Agent model call."""

from __future__ import annotations

import copy
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.models.conversation_task_context import ConversationTaskContextRecord


class AgentContextLoader:
    """Build a model context from persisted context records without owning state."""

    @staticmethod
    def load(
        task_id: int,
        context_rows: Sequence[ConversationTaskContextRecord],
    ) -> list[BaseMessage]:
        """Return included LangChain messages ordered by persisted sequence.

        Parameters:
            task_id: Task whose rows are being loaded.
            context_rows: Context records already read by the caller.

        Returns:
            Deep copies of rows with ``include_in_context=True`` ordered by ``sequence``.
            System messages are included; transport metadata is intentionally not attached to
            the returned model messages.

        Raises:
            ValueError: If a row belongs to another task or contains an unsupported message
                type. The loader does not interpret UI metadata or access persistence.

        Side effects:
            None. No cache, database write, runtime manager, or network access is used.
        """

        loaded: list[BaseMessage] = []
        for row in sorted(context_rows, key=lambda item: item.sequence):
            if row.task_id != task_id:
                raise ValueError("context row belongs to another task")
            if not row.include_in_context:
                continue
            if not isinstance(row.message, HumanMessage | AIMessage | ToolMessage | SystemMessage):
                raise ValueError("unsupported context message type")
            loaded.append(copy.deepcopy(row.message))
        return loaded


def load_agent_context(
    task_id: int,
    context_rows: Sequence[ConversationTaskContextRecord],
) -> list[BaseMessage]:
    """Functional entry point for :class:`AgentContextLoader`."""

    return AgentContextLoader.load(task_id, context_rows)
