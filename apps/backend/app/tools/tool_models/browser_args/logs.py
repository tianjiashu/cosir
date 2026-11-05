"""browser_logs：读页面控制台与网络日志。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.tools.tool_models.browser_args._shared import tab_field


# 页面日志：宿主按标签缓冲控制台消息与网络请求，条目带递增序号 seq；结果里的 next 是下次只读
# 新条目用的 since。
class BrowserLogsArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: Literal["console", "network"] = Field(
        description="console for console messages and uncaught errors, network for requests."
    )
    since: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Read entries from this sequence number on, oldest first; pass the next value of "
            "your last browser_logs result to read only new entries. Null returns the newest "
            "entries of the current page."
        ),
    )
    level: Literal["error", "warning", "info", "debug"] | None = Field(
        default=None,
        description="console only: the lowest level to include, e.g. warning for warnings and "
        "errors. Null includes all levels.",
    )
    failed_only: bool = Field(
        default=False,
        description="network only: only requests that failed or got a status of 400 or more.",
    )
    pattern: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Only entries whose text or URL contains this, ignoring case.",
    )
    limit: int = Field(default=50, ge=1, le=200, description="Maximum number of entries.")
    tab: str | None = tab_field()

    @model_validator(mode="after")
    def _filters_match_kind(self) -> "BrowserLogsArgs":
        if self.kind == "network" and self.level is not None:
            raise ValueError("level applies to console logs only")
        if self.kind == "console" and self.failed_only:
            raise ValueError("failed_only applies to network logs only")
        return self
