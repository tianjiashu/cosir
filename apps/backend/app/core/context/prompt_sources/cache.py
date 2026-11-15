"""读盘来源的进程级投影缓存：按文件签名判断是否重读，同一份配置的投影由全部 Session 共用。

每次模型请求前都要对账。整份重读在本仓实测约 50 ms（Skill 逐个解析 ``SKILL.md``、超长 AGENTS.md 截断），
占着全局解释器锁、随运行中的 Session 数叠加；而只做 stat 取签名不到 1 ms。所以每次读取先取签名（相关文件
与目录的修改时间、大小），签名没变就复用上次的投影，变了当场重读。手动编辑、外部工具改文件与配置页写入
都在下一次读取时被看到，不需要文件监听，也没有失效通知要维护。

签名规则见 ``app.utils.file_signature``。只缓存要读盘的来源；内存里的事实（Agent 预设、注册表、
``Settings``）由来源每次现读。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from app.core.context.prompt_sources.source import SourceItem
from app.utils.workspace_scope import normalize_scope_path


@dataclass(frozen=True)
class _Entry:
    signature: Hashable
    items: dict[str, SourceItem]


class ProjectionCache:
    """``(来源键, 作用域) → (签名, 投影)``；作用域由来源决定（工作区见 :func:`workspace_scope`，系统级为文件路径，
    Agent 预设为 Agent id）。

    同一个键的重读串行：多个 Session 同时发现签名变了，只有第一个读盘，其余直接用它的结果。不同键之间
    互不阻塞。返回的条目字典是共享对象，调用方不得修改。
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def get(
        self,
        source_key: str,
        scope: str,
        signature: Hashable,
        load: Callable[[], dict[str, SourceItem]],
    ) -> dict[str, SourceItem]:
        """签名与上次相同就返回缓存的投影，否则调用 ``load`` 重读并记下这次的签名。

        签名须在读盘之前取：读盘期间文件又被改了，下一次取到的签名就和记下的对不上，会再读一次。
        """

        key = (source_key, scope)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            entry = self._entries.get(key)
            if entry is not None and entry.signature == signature:
                return entry.items
            items = load()
            self._entries[key] = _Entry(signature, items)
            return items

    def drop_scope(self, workspace_root: str) -> None:
        """丢掉某个工作区的全部投影（workspace 被删除时调用，释放内存）；之后再读会按签名重建。"""

        scope = workspace_scope(workspace_root)
        with self._guard:
            for key in [key for key in self._entries if key[1] == scope]:
                self._entries.pop(key, None)


def workspace_scope(workspace_root: str) -> str:
    """工作区作用域的缓存键：与 Agent 注册表同一口径归一化，同一工作区的不同写法落到同一份缓存。"""

    return normalize_scope_path(workspace_root)


projection_cache = ProjectionCache()
"""进程级单例；作用域键带路径，数据根切换后自然不串用。"""
