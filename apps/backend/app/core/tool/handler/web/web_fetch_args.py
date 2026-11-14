"""web_fetch 工具的参数模型。

``urls`` 的模型可见长度上限取自 ``Constant.Web.FETCH_URL_LIMIT_MAX``（导入期求值，属固定
契约，不经环境变量覆盖）；handler 侧用同一常量做运行期二次校验，两处口径同源。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config.constant import Constant


class WebFetchArgs(BaseModel):
    """网页抓取工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    urls: list[str] = Field(
        min_length=1,
        max_length=Constant.Web.FETCH_URL_LIMIT_MAX,
        description=(
            f"Public HTTP(S) URLs to fetch, at most {Constant.Web.FETCH_URL_LIMIT_MAX} per call; "
            "put the pages you need together in one call. Duplicates are collapsed before any "
            "request. Do not put the user's personal details in a URL: it goes to an external "
            "service."
        ),
    )
    format: Literal["markdown", "html"] = Field(
        default="markdown",
        description=(
            "Content format. Use markdown for readable prose or html when you "
            "need the original markup."
        ),
    )
    char_limit: int | None = Field(
        default=None,
        ge=2000,
        description=(
            "Optional per-page character budget. "
            "Omit (null) to use the global default. Content beyond the budget is "
            "truncated and the result is marked truncated=true; when the combined "
            "output exceeds the global tool output budget it is saved to disk with a "
            "[output truncated; full output: <path>] marker you can read_file."
        ),
    )
