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

    def changes(self, state: Any) -> bool:
        """应用到 ``state`` 是否会改变它：等值 ``set``（含同一终态的重复投递）与空串追加都不改变。

        发布出口据此丢弃空操作，「重复投递由消费方幂等吸收」因此在出口一处兑现：前端不会因为等值
        变更换掉 part 对象、白算依赖身份复用的布局，各 planner 也不必各自比较新旧值。
        """

        if self.kind == "append-text":
            return self.value != ""
        parent: Any = state
        for key in self.path[:-1]:
            parent = parent[key]
        key = self.path[-1]
        if isinstance(parent, list):
            return not (isinstance(key, int) and key < len(parent) and parent[key] == self.value)
        return key not in parent or parent[key] != self.value

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
