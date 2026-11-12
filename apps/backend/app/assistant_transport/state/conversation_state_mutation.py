"""Conversation Transport snapshot mutation：一次可传输的快照变更，以及它在工作副本上的应用规则。"""

import copy
from dataclasses import dataclass
from typing import Any, Literal

MutationKind = Literal["set", "append-text"]
SnapshotPath = tuple[str | int, ...]


@dataclass(frozen=True)
class ConversationStateMutation:
    """描述一次可传输的 snapshot mutation；前端按同一规则应用（路径为完整历史里的位置）。"""

    kind: MutationKind
    path: SnapshotPath
    value: object

    def apply_to(self, state: Any) -> None:
        """把变更应用到 ``state``（快照工作副本，原地修改）。

        ``set`` 写入值的深拷贝（路径末端是列表长度时追加）；``append-text`` 把字符串拼到目标文本后。
        路径由投影函数产出、恒不为空。

        异常:
            ValueError: ``append-text`` 携带的不是字符串。
            TypeError: ``append-text`` 的目标不是字符串。
        """

        parent: Any = state
        for key in self.path[:-1]:
            parent = parent[key]
        key = self.path[-1]
        if self.kind == "set":
            value = copy.deepcopy(self.value)
            if isinstance(parent, list) and isinstance(key, int) and key == len(parent):
                parent.append(value)
            else:
                parent[key] = value
            return
        if not isinstance(self.value, str):
            raise ValueError("append-text mutation must carry a string")
        current = parent[key]
        if not isinstance(current, str):
            raise TypeError("append-text target must be a string")
        parent[key] = current + self.value


__all__ = ["ConversationStateMutation", "MutationKind", "SnapshotPath"]
