"""工作区文件变化的推送源：每个工作区一个递归原生监听，订阅只是过滤条件；通知仅使读取缓存失效，不维护文件事实。

为什么按工作区注册一次、而不是按目录登记：在后端进程里注册原生监听会占住 GIL（macOS 上 watchdog 与
watchfiles 实测都如此），注册期间整个进程冻住；系统文件事件服务繁忙时一次注册要 0.2–2 秒。按目录登记时前端每
展开一个目录就注册一次，冻结随操作次数增长。现在注册只在工作区第一个订阅到来时发生，展开目录只更新订阅的过滤
集合；最后一个订阅离开后延迟 ``IDLE_RELEASE_SECONDS`` 才释放，前端「先断旧连接、再建新连接」与关掉面板后很快
重开都不会触发重新注册。FSEvents 在系统层面本来就递归，递归监听在系统侧不更贵；Windows 上从每个目录一个句柄
变为根目录一个。根目录被改名或删除时递归监听直接报出根目录删除并停止，因此不再单独监听根的父目录。

原生事件只是尽力而为：系统不保证投递时限（fseventsd 满载时可迟到十几秒或丢失），由 watch 端点的周期对账兜底。
"""

import asyncio
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, ObservedWatch

from app.config.logging.logger import log
from app.service.depends import get_workspace_files_service
from app.utils.path.workspace_cosir import workspace_cosir_dir

IDLE_RELEASE_SECONDS = 60.0


@dataclass(eq=False)
class FileSubscription:
    """一条 watch 连接关心的目录（解析后的真实路径，恒含工作区根）；目录里的条目增删改即通知。"""

    workspace_id: int
    root: Path
    loop: asyncio.AbstractEventLoop
    changed: asyncio.Event
    paths: set[Path] = field(default_factory=set)
    polling: bool = False
    closed: bool = False

    def wants(self, path: Path) -> bool:
        return path in self.paths or path.parent in self.paths

    def notify(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.changed.set)


@dataclass(eq=False)
class _Registration:
    """一个工作区在观察器上的递归监听；``idle_since`` 为最后一个订阅离开的时刻，有订阅时为 None。"""

    watch: ObservedWatch
    idle_since: float | None = None


class _Handler(FileSystemEventHandler):
    def __init__(self, owner: "WorkspaceFileWatcher", workspace_id: int) -> None:
        self.owner, self.workspace_id = owner, workspace_id

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in {"created", "deleted", "modified", "moved", "closed"}:
            self.owner.changed(self.workspace_id, event)


class WorkspaceFileWatcher:
    """生命周期由 service 装配管理；连接关闭立即释放订阅，注册在工作区空闲 ``IDLE_RELEASE_SECONDS`` 后释放。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.files = get_workspace_files_service()
        self._clock = clock
        self._lock = threading.RLock()
        self._observer: BaseObserver | None = None
        self._registrations: dict[int, _Registration] = {}
        self._subscribers: set[FileSubscription] = set()
        self._timers: dict[int, threading.Timer] = {}

    def subscribe(
        self, workspace_id: int, paths: list[str], loop: asyncio.AbstractEventLoop
    ) -> FileSubscription:
        root = self.files.root(workspace_id).resolve()
        # 工作区 .cosir 里的浏览可见性配置变了，目录树也要刷新；它在根的递归范围内，只需列进过滤集合。
        targets = {root, workspace_cosir_dir(root)}
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
            registration = self._registrations.get(workspace_id)
            if registration is None:
                sub.polling = not self._register(workspace_id, root)
            else:
                registration.idle_since = None
                self._cancel_timer(workspace_id)
        return sub

    def _register(self, workspace_id: int, root: Path) -> bool:
        """在持锁状态下注册递归监听；失败时订阅转轮询，由周期对账兜底。注册耗时记进日志，用来观察进程冻结。"""
        started = time.monotonic()
        try:
            if self._observer is None:
                self._observer = Observer()
                self._observer.start()
            watch = self._observer.schedule(_Handler(self, workspace_id), str(root), recursive=True)
        except (OSError, SystemError) as exc:
            log.warning(
                "workspace_watch_degraded",
                extra={
                    "msg": "原生文件监听不可用，改用周期校验",
                    "data": {"workspace_id": workspace_id, "error": str(exc)},
                },
            )
            return False
        self._registrations[workspace_id] = _Registration(watch)
        log.info(
            "workspace_watch_registered",
            extra={
                "msg": "工作区原生监听已注册",
                "data": {
                    "workspace_id": workspace_id,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            },
        )
        return True

    def changed(self, workspace_id: int, event: FileSystemEvent) -> None:
        """原生事件入口：按事件路径（及移动的目标路径）找出关心它的订阅。根目录被删时根恒在订阅里，全部通知。"""
        paths = [Path(os.fsdecode(event.src_path))]
        if event.dest_path:
            paths.append(Path(os.fsdecode(event.dest_path)))
        with self._lock:
            for sub in self._subscribers:
                if sub.workspace_id == workspace_id and any(sub.wants(path) for path in paths):
                    sub.notify()

    def check_health(self, sub: FileSubscription) -> bool:
        """原生 emitter 可在线程内启动失败或因根目录变化停止；周期检查将其显式转为轮询状态。"""
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
        with self._lock:
            sub.closed = True
            self._subscribers.discard(sub)
            registration = self._registrations.get(sub.workspace_id)
            if registration is None or self._has_subscribers(sub.workspace_id):
                return
            registration.idle_since = self._clock()
            self._cancel_timer(sub.workspace_id)
            timer = threading.Timer(IDLE_RELEASE_SECONDS, self.release_idle)
            timer.daemon = True
            self._timers[sub.workspace_id] = timer
            timer.start()

    def release_idle(self) -> None:
        """释放空闲满 ``IDLE_RELEASE_SECONDS`` 的工作区注册；空闲计时器到期时调用，测试可注入时钟后直接调用。"""
        now = self._clock()
        with self._lock:
            idle = [
                workspace_id
                for workspace_id, registration in self._registrations.items()
                if registration.idle_since is not None
                and now - registration.idle_since >= IDLE_RELEASE_SECONDS
            ]
        for workspace_id in idle:
            self._release(workspace_id)

    def close_workspace(self, workspace_id: int) -> None:
        with self._lock:
            subscribers = [s for s in self._subscribers if s.workspace_id == workspace_id]
        for sub in subscribers:
            self.unsubscribe(sub)
            sub.notify()
        self._release(workspace_id)
        self.files.invalidate(workspace_id)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            self.unsubscribe(sub)
            sub.notify()
        with self._lock:
            workspaces = list(self._registrations)
        for workspace_id in workspaces:
            self._release(workspace_id)

    def _release(self, workspace_id: int) -> None:
        """撤销该工作区的注册；没有注册了就停掉观察器（join 在锁外，避免与事件线程互等）。"""
        observer = None
        with self._lock:
            self._cancel_timer(workspace_id)
            registration = self._registrations.pop(workspace_id, None)
            if registration is None or self._observer is None:
                return
            self._observer.unschedule(registration.watch)
            log.info(
                "workspace_watch_released",
                extra={"msg": "工作区原生监听已释放", "data": {"workspace_id": workspace_id}},
            )
            if not self._registrations:
                observer, self._observer = self._observer, None
                observer.stop()
        if observer:
            observer.join(timeout=5)

    def _has_subscribers(self, workspace_id: int) -> bool:
        return any(s.workspace_id == workspace_id for s in self._subscribers)

    def _cancel_timer(self, workspace_id: int) -> None:
        timer = self._timers.pop(workspace_id, None)
        if timer is not None:
            timer.cancel()
