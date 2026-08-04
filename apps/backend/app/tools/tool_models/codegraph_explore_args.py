"""codegraph_explore 工具参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class CodegraphExploreArgs(BaseModel):
    """codegraph_explore 查询参数（主工具，自然语言或符号/文件名集合）。

    参数:
        query: 符号名 / 文件名 / 短代码词集合，或自然语言问题。
        max_files: 最多返回源码的文件数上限。

    返回:
        Pydantic 参数模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    query: str = Field(
        description=(
            "Symbol names, file names, or short code terms to explore "
            '(e.g. "AuthService loginUser session-manager"). '
            "For a flow question, name the symbols spanning the flow "
            '(e.g. "mutateElement renderScene"). A natural-language question works too.'
        )
    )
    max_files: int = Field(
        default=12,
        ge=1,
        le=50,
        description="Maximum number of files to include source code from (default: 12).",
    )
