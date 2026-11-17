"""浏览器标签的回收：Session 删除后让宿主丢掉它的标签与下载，并在链路连上时对账补回收。

标签与下载归宿主（``BrowserHost``）。主路径是 Session 删除提交后 ``SessionService.finalize_deleted_session_spaces``
调 :func:`drop_sessions`，那是发后即忘的通知：宿主断线重连期间、后端刚就绪宿主还没连上、或提交后通知发出前
后端崩溃时都会丢，宿主上那些标签就一直跑到宿主退出（用户看不到也关不掉，还占标签上限，下载照写已删的工作区）。
所以链路每次连上（lifespan 经 ``HostLink.set_on_connected`` 接入），向宿主要一份持有资源的 Session，与库对账，
已删除的补一次通知。两个宿主方法名只在本模块出现。

Session id 自增不复用、删除不可逆，「已不存在」一经成立就不会变，所以对账与并发的创建、删除互不干扰，不需要闸门。
失败只记日志：下次连上再对。"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.service import depends as service_depends
from app.tools.browser_runtime.deadlines import host_timeout
from app.tools.host.errors import HostCallError
from app.tools.host.host_call import call_host

_HELD_METHOD = "browser.sessionIds"
_DROP_METHOD = "browser.dropSessions"
# 后台对账任务的强引用：事件循环只弱引用任务，不留引用可能被回收而中途消失。
_running: set[asyncio.Task[None]] = set()


def drop_sessions(session_ids: frozenset[int]) -> None:
    """通知宿主回收这些已删除 Session 的全部标签与下载；不等应答，丢了由连上时的对账补。"""

    service_depends.get_host_link().notify(_DROP_METHOD, {"session_ids": sorted(session_ids)})


def schedule_tab_reclaim() -> None:
    """链路连上时在事件循环上调用：排出一次对账，不阻塞链路读应答（对账自己的请求要靠它读回）。"""

    task = asyncio.get_running_loop().create_task(reclaim_orphan_tabs())
    _running.add(task)
    task.add_done_callback(_running.discard)


async def reclaim_orphan_tabs() -> None:
    """取宿主持有资源的 Session，已从库里删除的补发回收通知。"""

    try:
        result = await asyncio.to_thread(
            call_host,
            _HELD_METHOD,
            {},
            execution_context=None,
            timeout_seconds=host_timeout(_HELD_METHOD),
        )
        held = set(result["session_ids"])
        if not held:
            return
        existing = await asyncio.to_thread(
            service_depends.get_session_service().existing_session_ids, held
        )
    except HostCallError as exc:
        # 刚连上又断开或被顶替：下一条连接会再对一次。
        log.warning(
            "browser_tab_reclaim_skipped",
            extra={"msg": "浏览器标签回收对账未完成", "data": {"code": exc.code}},
        )
        return
    except Exception:
        log.exception("browser_tab_reclaim_failed", extra={"msg": "浏览器标签回收对账失败"})
        return
    orphans = sorted(held - existing)
    if not orphans:
        return
    log.info(
        "browser_tab_reclaimed",
        extra={
            "msg": "宿主仍持有已删除 Session 的标签或下载，补发回收",
            "data": {"session_ids": orphans},
        },
    )
    drop_sessions(frozenset(orphans))
