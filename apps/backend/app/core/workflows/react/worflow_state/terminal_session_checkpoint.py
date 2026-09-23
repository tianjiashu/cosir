"""单个终端 session 落 graph state 的元数据契约。

``ReactGraphState.terminal_sessions`` 的 value 类型：键为 session id，值为本结构。字段集合与
终端工具展示数据（``display_data.kind == "terminal-session"``）的投影 allowlist 一致，全部为
JSON 可序列化基本类型；活终端的真实状态由 backend 进程内 ``TerminalSessionService`` registry
负责，本结构只是它落 checkpoint 的一次快照，不驱动进程内生命周期。

value 显式声明为 ``TypedDict`` 后，Pydantic 会在 state 构造与 checkpoint 反序列化时按契约校验，
并剔除未声明的键——「哪些字段允许落 checkpoint」因此成为类型事实，而不是只写在写入方的白名单
注释里。本模块不 import 任何 app 内模块，避免与 ``state`` 互相引用成环。
"""

from typing import NotRequired

from typing_extensions import TypedDict


class TerminalSessionCheckpoint(TypedDict):
    """单个终端 session 落 checkpoint 的元数据（``terminal_sessions`` 的 value）。

    Attributes:
        session_id: 终端 session 标识，同时作为外层字典的 key。
        status: session 状态（``starting`` / ``running`` / ``closed`` 等）。
        initial_cwd: session 初始工作目录。
        shell_kind: shell 类型（如 ``pwsh`` / ``bash``）。
        first_available_seq: 输出 ring buffer 中仍可读取的最小序号。
        next_seq: 下一条输出序号。
        exit_code: 命令退出码；投影阶段会过滤 ``None``，此处容忍 ``None`` 以兼容历史 checkpoint。
        end_reason: session 结束原因；同上容忍 ``None``。

    除 ``session_id`` 外全部字段都可缺失：投影按「非 ``None`` 才写」逐字段合并，
    因此同一个 value 在不同事件之后可能只含其中一部分字段。
    """

    session_id: str
    status: NotRequired[str]
    initial_cwd: NotRequired[str]
    shell_kind: NotRequired[str]
    first_available_seq: NotRequired[int]
    next_seq: NotRequired[int]
    exit_code: NotRequired[int | None]
    end_reason: NotRequired[str | None]
