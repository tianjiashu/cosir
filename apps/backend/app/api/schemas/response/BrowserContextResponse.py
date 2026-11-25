"""内置浏览器标签上下文的 HTTP 投影。

字段按 HTTP 惯例保持 snake_case；前端（``lib/api/browser.ts``）换成宿主控制面契约的 camelCase
再交给宿主 ``browser.open``。
"""

from pydantic import BaseModel

from app.tools.browser_runtime.tab_context import BrowserTabContext


class BrowserTabLimitsResponse(BaseModel):
    per_session: int
    total: int


class BrowserContextResponse(BaseModel):
    limits: BrowserTabLimitsResponse
    download_dir: str
    display_session_id: int

    @classmethod
    def from_domain(cls, context: BrowserTabContext) -> "BrowserContextResponse":
        return cls(
            limits=BrowserTabLimitsResponse(
                per_session=context.limits.per_session, total=context.limits.total
            ),
            download_dir=context.download_dir,
            display_session_id=context.display_session_id,
        )
