"""web_search 工具的参数模型。

``limit`` 的默认值与模型可见上限取自 ``Constant.Web``（导入期求值，属固定契约，不经环境
变量覆盖；handler 形参默认值同源）。
"""

from pydantic import BaseModel, ConfigDict, Field

from app.config.constant import Constant


class WebSearchArgs(BaseModel):
    """网页搜索工具的模型可见参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        min_length=1,
        description=(
            "Search query to look up on the web. You may include backend-supported "
            "operators such as site:example.com, filetype:pdf, intitle:word, -term, "
            'or "exact phrase".'
        ),
    )
    limit: int = Field(
        default=Constant.Web.SEARCH_LIMIT_DEFAULT,
        ge=1,
        le=Constant.Web.SEARCH_LIMIT_MAX,
        description=(
            f"Maximum number of results to return. Defaults to "
            f"{Constant.Web.SEARCH_LIMIT_DEFAULT}, hard-capped by the runtime limit "
            f"({Constant.Web.SEARCH_LIMIT_MAX})."
        ),
    )
