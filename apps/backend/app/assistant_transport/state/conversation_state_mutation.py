"""Conversation Transport snapshot mutation 的中性类型。"""

from dataclasses import dataclass
from typing import Literal

MutationKind = Literal["set", "append-text"]
SnapshotPath = tuple[str | int, ...]


@dataclass(frozen=True)
class ConversationStateMutation:
    """描述一次可持久化、可传输的 snapshot mutation。"""

    kind: MutationKind
    path: SnapshotPath
    value: object
