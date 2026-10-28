"""终端工具的 UI 展示数据构造（纯函数，不执行 IO）。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.terminal_runtime.observation_render import TerminalObservationResult


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


def build_terminal_session_display_data(
    payload: Mapping[str, Any], *, include_terminal_info: bool = False
) -> dict[str, object]:
    """从 ``describe()`` 的完整输出投影工具卡片字段；动态会话状态以 snapshot 为准。"""

    fields: tuple[str, ...] = ("session_id", "status", "exit_code", "end_reason")
    if include_terminal_info:
        fields += ("initial_cwd", "shell_kind", "label")
    result: dict[str, object] = {"kind": "terminal-session"}
    result.update({key: payload[key] for key in fields})
    if include_terminal_info:
        result["integration"] = payload["integration"]["status"]
    return result


def build_terminal_session_list_display_data(session_count: int) -> dict[str, object]:
    """只展示本次发现数量；会话实时状态由 snapshot 的 terminal_sessions 提供。"""

    return {"kind": "terminal-session", "session_count": session_count}


def build_terminal_observation_display_data(
    result: TerminalObservationResult,
) -> dict[str, object]:
    """write / read 动作行：会话、实际输入摘要、等待结果与返回行范围；不承载终端输出。"""

    data: dict[str, object] = {
        "kind": "terminal-session",
        "session_id": result.session_id,
        "status": result.status,
        "lines": list(result.lines) if result.lines is not None else None,
        "wait_result": dict(result.wait_result) if result.wait_result is not None else None,
        "suspended": result.suspended,
    }
    if result.input_type is not None:
        data["input_type"] = result.input_type
        data["input_summary"] = result.input_summary
    return data
