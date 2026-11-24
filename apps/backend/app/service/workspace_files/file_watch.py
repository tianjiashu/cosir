"""工作区文件变化的推送源：按周期比对用户看得见的路径，变了就通知前端重读；不维护文件事实，也不让后端缓存失效。

为什么轮询、不用原生监听：在后端进程里注册原生监听（watchdog / watchfiles 底层的 FSEvents）时，C 扩展等 fseventsd
答复期间不放 GIL，整个后端冻住；系统繁忙时实测一次 18 秒，文件面板「加载中」、Agent 输出与全部接口一起卡住。
需求本身也不是监听整个仓库：前端只登记画面上用得到的路径（展开的目录、打开的标签、Markdown 图片、交付物），
几十到几百个路径每秒 stat 一遍是毫秒级，不经过系统文件事件服务，三个平台行为一致。

比对什么：每个路径记文件面板的版本 ``version_of``（设备、inode、大小、mtime、ctime），不存在记 ``None``；
前端正是按这个版本重读，所以「通知」与「版本变了」是同一个事实，不另定规则。目录的条目增删、改名会改它的
版本；ctime 由系统维护、改不回去，保留 mtime 的写入（``cp -p`` 覆盖、改写后恢复 mtime）也逃不掉——曾经只比
``(inode, mtime, size)``，这类写入不通知。展开目录里没打开的文件改了内容不会通知：文件树行不显示大小与修改时间。

不负责：连接寿命（客户端断开由 Starlette 的流式响应取消协程）、工作区删除（界面卸载即断开连接）。
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from app.config.logging.logger import log
from app.service.workspace_files.content.file_content import version_of
from app.service.workspace_files.workspace_files_service import WorkspaceFilesService
from app.service.workspace_files.workspace_paths import resolve_path
from app.utils.path.workspace_cosir import workspace_file_browser_settings_file

POLL_INTERVAL_SECONDS = 1.0
# 一次比对超过这个时长就记日志：用来发现登记路径异常多或磁盘异常慢，不影响行为。
SLOW_POLL_SECONDS = 0.1

Stamp = str | None


class WatchedPaths:
    """一条 watch 连接盯着的路径与上一次看到的状态；只在单个连接的协程里使用，不跨连接共享。"""

    def __init__(self, workspace_id: int, paths: Iterable[Path]) -> None:
        self.workspace_id = workspace_id
        self.paths = tuple(sorted(set(paths)))
        # 基准在构造时取：必须早于推给前端的 ready，否则 ready 之后、基准之前的变化会被漏掉。
        self._last = self._stamps()

    @classmethod
    def for_request(
        cls, files: WorkspaceFilesService, workspace_id: int, relatives: Iterable[str]
    ) -> "WatchedPaths":
        """按前端登记的相对路径建立；工作区不存在时 ``files.root`` 抛 ``KeyError``，由 HTTP 边界映射为 404。

        恒含根目录（根被删或改名时前端据此重读并显示工作区不在）与浏览可见性配置文件（它改了目录树要重算）。
        还不存在的路径按根下的字面位置盯着，出现即通知；非法或越界的路径跳过——内容接口负责显示条目错误，
        一个坏路径不能让其余路径失去更新。
        """
        root = files.root(workspace_id)
        paths = {root, workspace_file_browser_settings_file(root)}
        for relative in relatives:
            try:
                paths.add(resolve_path(root, relative))
            except FileNotFoundError:
                # resolve_path 先校验写法再解析，走到这里说明写法合法、只是目标还不在。
                paths.add(root / relative)
            except (ValueError, OSError):
                continue
        watched = cls(workspace_id, paths)
        log.info(
            "workspace_watch_started",
            extra={
                "msg": "工作区文件轮询已开始",
                "data": {"workspace_id": workspace_id, "paths": len(watched.paths)},
            },
        )
        return watched

    def poll(self) -> bool:
        """比对一次并更新基准；返回自上次比对以来是否有变化。阻塞的磁盘调用，调用方放进工作线程。"""
        started = time.monotonic()
        current = self._stamps()
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_POLL_SECONDS:
            log.warning(
                "workspace_watch_slow_poll",
                extra={
                    "msg": "工作区文件轮询较慢",
                    "data": {
                        "workspace_id": self.workspace_id,
                        "paths": len(self.paths),
                        "duration_ms": round(elapsed * 1000),
                    },
                },
            )
        changed, self._last = current != self._last, current
        return changed

    async def changes(self) -> AsyncIterator[None]:
        """每个周期比对一次，有变化时产出一次；同一周期内的多次变化自然合并为一次。"""
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            if await asyncio.to_thread(self.poll):
                yield None

    def _stamps(self) -> tuple[Stamp, ...]:
        return tuple(self._stamp(path) for path in self.paths)

    @staticmethod
    def _stamp(path: Path) -> Stamp:
        try:
            return version_of(os.stat(path))
        except OSError:
            return None
