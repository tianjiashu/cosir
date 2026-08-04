"""codegraph_callers 工具参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class CodegraphCallersArgs(BaseModel):
    """codegraph_callers 查询参数（列出谁调用给定符号的函数）。

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
        description="Name of the function, method, or class to find callers for."
    )
    file: str | None = Field(
        default=None,
        description=(
            "Narrow to the definition in this file (path or suffix) when several same-named "
            "symbols exist (e.g. one UserService per app in a monorepo)."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of callers to return (default: 20).",
    )
