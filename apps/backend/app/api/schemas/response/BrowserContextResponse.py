"""内置浏览器标签上下文的 HTTP 投影。

字段按 HTTP 惯例保持 snake_case；前端（``lib/api/browser.ts``）换成宿主控制面契约的 camelCase
再交给宿主 ``browser.open``。
"""

from pydantic import BaseModel

from app.service.browser.tab_context import BrowserTabContext


class BrowserTabLimitsResponse(BaseModel):
    per_task: int
    total: int


class BrowserContextResponse(BaseModel):
    limits: BrowserTabLimitsResponse
    download_dir: str

    @classmethod
    def from_domain(cls, context: BrowserTabContext) -> "BrowserContextResponse":
        return cls(
            limits=BrowserTabLimitsResponse(
                per_task=context.limits.per_task, total=context.limits.total
            ),
            download_dir=context.download_dir,
        )
