"""``tool_search`` 的参数模型；两个参数的说明与本仓对齐的 ToolSearch 原文一致，只换了工具名。"""

from pydantic import BaseModel, ConfigDict, Field

MAX_RESULTS_DEFAULT = 5


class ToolSearchArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        min_length=1,
        description=(
            'Query to find deferred tools. Use "select:<tool_name>" for direct selection, or '
            "keywords to search."
        ),
    )
    max_results: int = Field(
        default=MAX_RESULTS_DEFAULT,
        ge=1,
        description=f"Maximum number of results to return (default: {MAX_RESULTS_DEFAULT})",
    )
