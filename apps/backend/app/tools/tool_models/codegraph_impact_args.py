"""codegraph_impact 工具参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class CodegraphImpactArgs(BaseModel):
    """codegraph_impact 查询参数（列出改动某符号会影响哪些符号）。

    参数:
        symbol: 待分析影响的符号名。
        file: 同名符号多时用文件路径/后缀锁定。
        depth: 依赖遍历层数。

    返回:
        Pydantic 参数模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    symbol: str = Field(description="Name of the symbol to analyze impact for.")
    file: str | None = Field(
        default=None,
        description=(
            "Narrow to the definition in this file (path or suffix) when several same-named "
            "symbols exist."
        ),
    )
    depth: int = Field(
        default=2,
        ge=1,
        le=10,
        description="How many levels of dependencies to traverse (default: 2).",
    )
