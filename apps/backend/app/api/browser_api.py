"""内置浏览器 HTTP 接口：用户在面板里新开标签前，取宿主需要的标签上下文。

标签本身归宿主（``window.cosirHost.browser``），后端只提供宿主不知道的事实；解析规则与 Agent 路径
共用 ``app.service.browser.tab_context``。
"""

from fastapi import HTTPException

from app.api.schemas.response.BrowserContextResponse import BrowserContextResponse
from app.app import app
from app.service.browser.tab_context import BrowserTabContext


@app.get("/sessions/{session_id}/browser-context", response_model=BrowserContextResponse)
async def get_browser_context(session_id: int) -> BrowserContextResponse:
    """返回该 Session 的标签上限与下载目录；Session 不存在时 404。"""

    try:
        context = BrowserTabContext.from_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    return BrowserContextResponse.from_domain(context)
