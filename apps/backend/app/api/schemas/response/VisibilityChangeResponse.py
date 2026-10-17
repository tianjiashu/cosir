"""浏览器忽略设置变更投影；与领域 VisibilityChange 对应。"""

from pydantic import BaseModel

from app.api.schemas.response.BrowserVisibilityResponse import BrowserVisibilityResponse
from app.service.workspace_files.browser_visibility import VisibilityChange


class VisibilityChangeResponse(BaseModel):
    settings: BrowserVisibilityResponse
    changed_paths: list[str]

    @classmethod
    def from_domain(cls, change: VisibilityChange) -> "VisibilityChangeResponse":
        return cls(
            settings=BrowserVisibilityResponse.from_domain(change.settings),
            changed_paths=list(change.changed_paths),
        )
