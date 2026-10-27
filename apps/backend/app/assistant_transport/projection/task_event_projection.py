"""Task 级事件的 snapshot 投影。"""

from collections.abc import Sequence

from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.core.events import TaskTitleUpdatedEvent


def plan_task_title_updated(
    event: TaskTitleUpdatedEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """标题与投影一致时不发 mutation，重复投递因此天然幂等。"""

    if state["title"] == event.title:
        return ()
    return (ConversationStateMutation("set", ("title",), event.title),)
