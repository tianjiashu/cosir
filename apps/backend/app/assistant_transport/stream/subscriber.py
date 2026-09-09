"""进程内 snapshot 订阅者。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.assistant_transport.stream.snapshot_change import SnapshotChange


@dataclass(frozen=True)
class _Subscriber:
    """绑定事件循环的进程内订阅者。"""

    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[SnapshotChange]
