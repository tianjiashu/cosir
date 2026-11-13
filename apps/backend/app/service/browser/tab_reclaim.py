"""浏览器标签的回收对账：补上丢掉的 Session 删除通知。

标签与下载归宿主（``BrowserHost``）。Session 删除提交后由 ``SessionService.finalize_deleted_session_spaces`` 发
``browser.dropSessions``，那是发后即忘的通知：宿主断线重连期间、后端刚就绪宿主还没连上、或提交后通知发出前
后端崩溃时都会丢，宿主上那些标签就一直跑到宿主退出（用户看不到也关不掉，还占标签上限，下载照写已删的工作区）。
所以链路每次连上（lifespan 经 ``HostLink.set_on_connected`` 接入），向宿主要一份持有资源的 Session，与库对账，
已删除的补一次通知。删除时的即时通知仍是主路径，这里只在连接这一处兜底。

Session id 自增不复用、删除不可逆，「已不存在」一经成立就不会变，所以对账与并发的创建、删除互不干扰，不需要闸门。
失败只记日志：下次连上再对。
"""

from __future__ import annotations

import asyncio

from app.config.logging.logger import log
from app.service import depends as service_depends
from app.tools.host.errors import HostCallError
from app.tools.host.host_call import call_host
from app.tools.tool_handler.browser.host_request import host_timeout

_HELD_METHOD = "browser.sessionIds"
# 后台对账会话的强引用：事件循环只弱引用会话，不留引用可能被回收而中途消失。
_running: set[asyncio.Task[None]] = set()


def schedule_tab_reclaim() -> None:
    """链路连上时在事件循环上调用：排出一次对账，不阻塞链路读应答（对账自己的请求要靠它读回）。"""

    task = asyncio.get_running_loop().create_task(reclaim_orphan_tabs())
    _running.add(task)
    task.add_done_callback(_running.discard)


async def reclaim_orphan_tabs() -> None:
    """取宿主持有资源的 Session，已从库里删除的补发 ``browser.dropSessions``。"""

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
    service_depends.get_host_link().notify("browser.dropSessions", {"session_ids": orphans})
