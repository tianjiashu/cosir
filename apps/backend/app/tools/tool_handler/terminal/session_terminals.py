"""一个 Session 的终端：用户打开的会话与 Agent 自己运行中的命令，查找与列举只此一份。

两类终端分属两个集合（``TerminalSessionService`` 与 ``running_commands``），Agent 工具把它们当成
一张表：列举与查找都是「会话在前、命令在后」。这是 Agent 视角的组合（用户路径的工作台与 WS 只认
会话），所以放在工具一侧，不进运行时。
"""

from __future__ import annotations

from app.service import depends as service_depends
from app.tools.terminal_runtime.command.running_command_registry import running_commands
from app.tools.terminal_runtime.errors import TerminalNotFoundError, TerminalSessionNotFoundError
from app.tools.terminal_runtime.observation.observed_terminal import ObservedTerminal


def find_terminal(session_id: int, terminal_id: str) -> ObservedTerminal:
    """按 id 找本 Session 的终端：先查用户会话，再查运行中的命令（顺序固定）。

    两者理论上可能同名（会话 id 是 ``term_`` + 12 位十六进制，命令 id 由模型厂商给出并在冲突时改写），
    约定会话优先；不同 id 形态下这种碰撞可以忽略，不另加前缀。

    异常:
        TerminalNotFoundError: 两处都没有（已结束的命令结论走 ``<background_result>``，不再可读）。
        TerminalSessionOwnershipError: 会话存在但不属于该 Session。
    """
    try:
        return service_depends.get_terminal_session_service().require(terminal_id, session_id)
    except TerminalSessionNotFoundError:
        pass
    try:
        return running_commands.get(session_id, terminal_id)
    except KeyError:
        raise TerminalNotFoundError(
            f"no terminal {terminal_id} in this session: neither a terminal the user opened nor a "
            "command of yours that is still running (a finished command's result arrives as a "
            "<background_result> message)"
        ) from None


def terminal_summaries(session_id: int) -> list[str]:
    """本 Session 每个终端的一行摘要，会话在前、命令在后；状态措辞与读取结果的表头同源。"""
    sessions = [
        runtime.snapshot().summary
        for runtime in service_depends.get_terminal_session_service().session_runtimes(session_id)
    ]
    commands = [command.summary() for command in running_commands.session_commands(session_id)]
    return [*sessions, *commands]
