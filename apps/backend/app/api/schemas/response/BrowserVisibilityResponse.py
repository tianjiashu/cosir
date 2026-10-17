"""浏览器忽略设置投影；与领域 BrowserVisibility 对应。"""

from pydantic import BaseModel

from app.service.workspace_files.browser_visibility import BrowserVisibility


class BrowserVisibilityResponse(BaseModel):
    ignored_paths: list[str]
    revision: str

    @classmethod
    def from_domain(cls, visibility: BrowserVisibility) -> "BrowserVisibilityResponse":
        return cls(
            ignored_paths=list(visibility.ignored_paths),
            revision=visibility.revision,
        )
