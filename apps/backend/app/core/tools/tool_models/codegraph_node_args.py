"""codegraph_node 工具参数模型。"""

from pydantic import BaseModel, ConfigDict, Field


class CodegraphNodeArgs(BaseModel):
    """codegraph_node 查询参数（双模式：读文件 或 读单符号，替代 Read）。

    参数:
        symbol: 符号模式，要读的符号名。
        file: 文件路径或 basename；单独传 = 读整个文件，与 symbol 同传 = 消歧。
        include_code: 符号模式是否包含完整源码体。
        offset: 文件模式起始行。
        limit: 文件模式最大行数。
        symbols_only: 文件模式只返回符号表 + 依赖者。
        line: 符号模式，配合 file:line 锁定歧义符号。

    返回:
        Pydantic 参数模型。

    异常:
        无。

    副作用:
        无。
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    symbol: str | None = Field(
        default=None,
        description=(
            "Symbol to read (symbol mode). Omit it and pass `file` alone to read a whole "
            "file like Read."
        ),
    )
    file: str | None = Field(
        default=None,
        description=(
            'A file path or basename (e.g. "harness.rs", "src/auth/session.ts"). Pass it ALONE '
            "(no symbol) to READ the file like Read — its full source with line numbers + which "
            "files depend on it. Or pass it WITH a symbol to disambiguate an overloaded name."
        ),
    )
    include_code: bool = Field(
        default=False,
        description=(
            "Symbol mode: include the symbol's full body (default: false). "
            "Ignored in file mode, which always returns source unless symbols_only is set."
        ),
    )
    offset: int | None = Field(
        default=None,
        ge=1,
        description="File mode: 1-based line to start reading from, like Read's offset.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=2000,
        description="File mode: maximum lines to return (capped at 2000, like Read).",
    )
    symbols_only: bool = Field(
        default=False,
        description=(
            "File mode: return just the file's symbol map + dependents (a cheap structural "
            "overview) instead of its source."
        ),
    )
    line: int | None = Field(
        default=None,
        ge=1,
        description="Symbol mode only: disambiguate to the definition at/around this line.",
    )
