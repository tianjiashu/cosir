"""codegraph_callees 工具参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class CodegraphCalleesArgs(BaseModel):
    """codegraph_callees 查询参数（列出给定符号调用谁）。

    参数:
        symbol: 函数 / 方法 / 类名。
        file: 同名符号多时用文件路径/后缀锁定。
        limit: 最大返回数。

    返回:
        Pydantic 参数模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    symbol: str = Field(
        description="Name of the function, method, or class to find callees for."
    )
    file: str | None = Field(
        default=None,
        description=(
            "Narrow to the definition in this file (path or suffix) when several same-named "
            "symbols exist."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of callees to return (default: 20).",
    )
