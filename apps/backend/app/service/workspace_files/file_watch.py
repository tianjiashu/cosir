"""按订阅共享非递归监听；通知仅使读取缓存失效，不维护文件事实。"""

import asyncio
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, ObservedWatch

from app.config.logging.logger import log
from app.service.depends import get_workspace_files_service


@dataclass(eq=False)
class FileSubscription:
    workspace_id: int
    root: Path
    loop: asyncio.AbstractEventLoop
    changed: asyncio.Event
    paths: set[Path] = field(default_factory=set)
    polling: bool = False
    closed: bool = False

    def notify(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.changed.set)


class _Handler(FileSystemEventHandler):
    def __init__(self, owner: "WorkspaceFileWatcher", path: Path) -> None:
        self.owner, self.path = owner, path

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"created", "deleted", "modified", "moved", "closed"}:
            self.owner.changed(self.path, event)


class WorkspaceFileWatcher:
    """生命周期由 service 装配管理；连接关闭立即释放订阅，最后一个订阅释放观察器。"""

    def __init__(self) -> None:
        self.files = get_workspace_files_service()
        self._lock = threading.RLock()
        self._observer: BaseObserver | None = None
        self._watches: dict[Path, ObservedWatch] = {}
        self._subscribers: set[FileSubscription] = set()

    def subscribe(
        self, workspace_id: int, paths: list[str], loop: asyncio.AbstractEventLoop
    ) -> FileSubscription:
        root = self.files.root(workspace_id)
        # 根的父目录让整个 workspace 被移走后仍能收到通知。
        targets = {root, root.parent}
        # 忽略配置可在编辑器中原子替换；目录不存在时由根监听和周期校验覆盖首次创建。
        settings_dir = root / ".cosir"
        if settings_dir.is_dir():
            targets.add(settings_dir)
        for relative in paths:
            try:
                path = self.files.path(workspace_id, relative)
                targets.add((path if path.is_dir() else path.parent).resolve(strict=True))
            except OSError:
                # 内容接口负责显示条目错误；监听缺失不能阻止其余目录更新。
                continue
        sub = FileSubscription(workspace_id, root, loop, asyncio.Event(), targets)
        with self._lock:
            self._subscribers.add(sub)
            try:
                if self._observer is None:
                    self._observer = Observer()
                    self._observer.start()
                for path in targets:
                    if path not in self._watches:
                        if len(self._watches) >= 128:
                            sub.polling = True
                            continue
                        self._watches[path] = self._observer.schedule(
                            _Handler(self, path), str(path), recursive=False
                        )
            except (OSError, SystemError) as exc:
                sub.polling = True
                log.warning(
                    "workspace_watch_degraded",
                    extra={
                        "msg": "原生文件监听不可用，改用周期校验",
                        "data": {"workspace_id": workspace_id, "error": str(exc)},
                    },
                )
        return sub

    def changed(self, path: Path, event: FileSystemEvent) -> None:
        with self._lock:
            for sub in self._subscribers:
                if path == sub.root.parent and str(sub.root) not in {
                    os.fsdecode(event.src_path),
                    os.fsdecode(event.dest_path),
                }:
                    continue
                if path in sub.paths:
                    sub.notify()

    def check_health(self, sub: FileSubscription) -> bool:
        """原生 emitter 可在线程内启动失败；周期检查将其显式转为轮询状态。"""
        with self._lock:
            if self._observer and any(
                not emitter.is_alive() for emitter in self._observer.emitters
            ):
                if not sub.polling:
                    log.warning(
                        "workspace_watch_degraded",
                        extra={
                            "msg": "原生监听线程已退出，改用周期校验",
                            "data": {"workspace_id": sub.workspace_id},
                        },
                    )
                sub.polling = True
            return sub.polling

    def unsubscribe(self, sub: FileSubscription) -> None:
        observer = None
        with self._lock:
            sub.closed = True
            self._subscribers.discard(sub)
            used = set().union(*(s.paths for s in self._subscribers))
            if self._observer:
                for path in list(self._watches):
                    if path not in used:
                        self._observer.unschedule(self._watches.pop(path))
                if not self._subscribers:
                    observer, self._observer = self._observer, None
                    observer.stop()
        if observer:
            observer.join(timeout=5)

    def close_workspace(self, workspace_id: int) -> None:
        with self._lock:
            subscribers = [s for s in self._subscribers if s.workspace_id == workspace_id]
        for sub in subscribers:
            self.unsubscribe(sub)
            sub.notify()
        self.files.invalidate(workspace_id)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            self.unsubscribe(sub)
            sub.notify()
