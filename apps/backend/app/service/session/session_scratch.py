"""会话临时目录的生命周期：驱动开始前建好，会话删除提交后回收，后端启动时对账补回收。

目录位置只由会话 id 推导（``session_scratch_dir``），不入库；读写权限归 ``ProtectedPaths`` 与
``PathResolver``，这里只管目录在不在。

回收是尽力而为，失败只记日志、不上浮：会话行已经删掉，没有可以回滚的；残留目录由下次启动的对账删除。
Windows 上最容易失败（文件被进程占用、被杀毒或索引扫描锁住、带只读属性，git 对象文件就是只读的），
所以删除时先去掉只读属性再重试一次，仍失败就留给对账。
"""

from __future__ import annotations

import os
import shutil
import stat
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from app.config.logging.logger import log
from app.utils.path.system_cosir import session_scratch_dir, system_scratch_dir


def ensure_session_scratch(session_id: int) -> None:
    """建好会话的临时目录（已存在不动）。

    每次驱动开始前调用：终端的第一条命令就可能 cd 进去，写类文件工具也只认已存在的临时目录。建不出来
    只记日志：临时目录是辅助能力，不能因此让 Run 失败；Agent 写进去时会得到明确的拒绝。
    """

    try:
        session_scratch_dir(session_id).mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning(
            "session_scratch_create_failed",
            extra={"msg": "会话临时目录创建失败", "data": {"session_id": session_id}},
            exc_info=True,
        )


def remove_session_scratch(session_ids: Iterable[int]) -> None:
    """在后台线程里删除这些会话的临时目录，发后即忘。

    调用方（会话删除提交后的收尾）还持着 workspace 闸门，目录可能很大，不能在闸门里等删除。线程设为
    daemon：进程退出时没删完的，由下次启动的对账接着删；会话 id 不复用，迟到的删除不会误删新会话的目录。
    """

    targets = [(session_scratch_dir(session_id), session_id) for session_id in session_ids]
    if targets:
        threading.Thread(target=_remove_all, args=(targets,), name="session-scratch-remove", daemon=True).start()


def reclaim_orphan_scratch(existing_session_ids: Callable[[set[int]], set[int]]) -> None:
    """在后台线程里删除已不存在的会话留下的临时目录（崩溃时没来得及回收、或回收失败的）。

    ``existing_session_ids`` 从候选 id 里筛出仍存在的会话（``SessionService.existing_session_ids``），
    在调用线程里先查完，后台线程只删目录。名字不是会话 id 的条目不归本模块，不碰。
    """

    root = system_scratch_dir()
    if not root.is_dir():
        return
    candidates = {int(entry.name): entry for entry in root.iterdir() if entry.is_dir() and entry.name.isdigit()}
    orphans = sorted(set(candidates) - existing_session_ids(set(candidates)))
    if not orphans:
        return
    log.info(
        "session_scratch_orphans_found",
        extra={"msg": "发现孤立的会话临时目录，后台删除", "data": {"session_ids": orphans}},
    )
    threading.Thread(
        target=_remove_all,
        args=([(candidates[session_id], session_id) for session_id in orphans],),
        name="session-scratch-reclaim",
        daemon=True,
    ).start()


def _remove_all(targets: list[tuple[Path, int]]) -> None:
    for directory, session_id in targets:
        _remove(directory, session_id)


def _remove(directory: Path, session_id: int) -> None:
    if not directory.exists():
        return
    try:
        shutil.rmtree(directory, onerror=_clear_readonly_and_retry)
        log.info(
            "session_scratch_removed",
            extra={"msg": "会话临时目录已删除", "data": {"session_id": session_id}},
        )
    except OSError:
        log.warning(
            "session_scratch_remove_failed",
            extra={
                "msg": "会话临时目录删除失败，留给下次启动对账",
                "data": {"session_id": session_id, "path": str(directory)},
            },
            exc_info=True,
        )


def _clear_readonly_and_retry(function: Callable[[str], object], path: str, _exc_info: object) -> None:
    """Windows 上只读文件删不掉：去掉只读属性再删一次；仍失败就让异常上浮到 ``_remove``。

    用 ``onerror`` 而不是 3.12 的 ``onexc``：项目最低支持 Python 3.11。
    """

    os.chmod(path, stat.S_IWRITE)
    function(path)
