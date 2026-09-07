"""web_extract 工具的参数模型。

本模块在导入期读取 ``Settings.WEB_EXTRACT_URL_LIMIT_MAX`` 生成 schema 上限，与
``HandlerBase.timeout_seconds`` 一样属进程启动期静态配置：环境变量变更后需重启
后端进程才会反映到模型可见 schema（运行时仍以 ``Settings`` 为准做二次校验）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config.settings import Settings


class WebExtractArgs(BaseModel):
    """网页正文提取工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    urls: list[str] = Field(
        min_length=1,
        max_length=Settings.WEB_EXTRACT_URL_LIMIT_MAX,
        description=(
            f"List of public HTTP(S) URLs to extract content from "
            f"(max {Settings.WEB_EXTRACT_URL_LIMIT_MAX} URLs per call). "
            "Duplicates are collapsed before any network request."
        ),
    )
    format: Literal["markdown", "html"] = Field(
        default="markdown",
        description=(
            "Extraction format. Use markdown for readable prose or html when you "
            "need the original markup; both must be supported by the provider."
        ),
    )
    char_limit: int | None = Field(
        default=None,
        ge=2000,
        description=(
            "Optional per-page character budget applied by the extraction provider. "
            "Omit (null) to use the global default. Content beyond the budget is "
            "truncated and the result is marked truncated=true; when the combined "
            "output exceeds the global tool output budget it is saved to disk with a "
            "[output truncated; full output: <path>] marker you can read_file."
        ),
    )
