"""终端工具的 UI 展示数据构造（纯函数，不执行 IO）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.terminal_runtime.render import TerminalObservationResult
    from app.tools.terminal_runtime.session_runtime import SessionSnapshot


def build_terminal_display_data(
    *,
    command: str,
    workdir: Path,
    output: str,
    exit_code: int,
    timed_out: bool,
) -> dict[str, Any]:
    """构造完整的终端展示数据。

    命令和输出均按调用方提供的原文展示；输出保留 ANSI 控制序列。
    本函数不执行命令、不截断输出，也不负责工具状态判断。
    """

    return {
        "kind": "terminal-result",
        "command": command,
        "workdir": str(workdir),
        "output": output,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def build_terminal_background_display_data(*, command: str, workdir: Path) -> dict[str, Any]:
    """命令转到后台时占位卡片的展示数据：还没有输出与退出码，``background`` 标明在后台运行。

    卡片状态仍以 part 生命周期为准（占位为 ``pending``，结论并入或撤销时推到终态）；结论正文只回投
    给模型，不回写这里。
    """

    return {"kind": "terminal-result", "command": command, "workdir": str(workdir), "background": True}


def build_terminal_session_display_data(
    session: SessionSnapshot, *, include_terminal_info: bool = False
) -> dict[str, object]:
    """start / close 卡片实际读取的字段；之后的动态会话状态以 Transport 会话列表为准。"""

    result: dict[str, object] = {
        "kind": "terminal-session",
        "session_id": session.session_id,
        "status": session.status,
        "exit_code": session.exit_code,
    }
    if include_terminal_info:
        result.update(
            initial_cwd=session.initial_cwd, shell_kind=session.shell_kind, label=session.label
        )
    return result


def build_terminal_session_list_display_data(session_count: int) -> dict[str, object]:
    """只展示本次发现数量；会话实时状态由 snapshot 的 terminal_sessions 提供。"""

    return {"kind": "terminal-session", "session_count": session_count}


def build_terminal_observation_display_data(
    result: TerminalObservationResult,
) -> dict[str, object]:
    """write / read / handoff 动作行：会话、实际输入摘要（交接为原因）、等待结果与返回行范围；不承载
    终端输出。

    全屏程序期间返回的是整屏（``screen``），没有逻辑行区间。交接是否仍在等待不在这里表达：卡片读
    工具 part 的状态（等待占位为 ``pending``）。
    """

    data: dict[str, object] = {
        "kind": "terminal-session",
        "session_id": result.session_id,
        "status": result.status,
        "lines": list(result.lines) if result.lines is not None else None,
        "screen": result.screen,
        "wait_result": dict(result.wait_result) if result.wait_result is not None else None,
    }
    if result.input_summary is not None:
        data["input_summary"] = result.input_summary
    return data
