"""codegraph_search 工具参数模型。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CodegraphSearchArgs(BaseModel):
    """codegraph_search 查询参数（按名快速符号搜索，仅返回位置无代码）。

    参数:
        query: 符号名或部分名。
        kind: 按节点类型过滤。
        limit: 最大结果数。

    返回:
        Pydantic 参数模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        description='Symbol name or partial name to search (e.g. "auth", "signIn", "UserService").'
    )
    kind: Literal[
        "function",
        "method",
        "class",
        "interface",
        "type",
        "variable",
        "route",
        "component",
    ] | None = Field(
        default=None,
        description=(
            "Filter results by node kind "
            "(function/method/class/interface/type/variable/route/component)."
        ),
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum number of results to return (default: 10).",
    )
