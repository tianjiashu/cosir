"""终端工具的 UI 展示数据构造（纯函数，不执行 IO）。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.tools.terminal_runtime.observation.render import TerminalObservationResult


def build_terminal_display_data(
    *,
    command: str,
    workdir: Path,
    output: str,
    end: str,
    exit_code: int | None,
    signal: int | None,
    omitted_lines: int,
    log_path: str,
) -> dict[str, Any]:
    """构造完整的终端展示数据。

    命令和输出均按调用方提供的原文展示；输出保留 ANSI 控制序列，是原始输出的最近一段（窗口规则见
    ``core/tool/terminal_output_window.py``），完整输出在 ``output_log_path``。``end`` 是执行结局
    （``CommandEnd``），``exit_code`` / ``signal`` 只在命令自己结束时出现。本函数不执行命令、不截断
    输出，也不负责工具状态判断；省略行数与日志路径只在该有值时出现。
    """

    data: dict[str, Any] = {
        "kind": "terminal-result",
        "command": command,
        "workdir": str(workdir),
        "output": output,
        "end": end,
    }
    if exit_code is not None:
        data["exit_code"] = exit_code
    if signal is not None:
        data["signal"] = signal
    if omitted_lines > 0:
        data["output_omitted_lines"] = omitted_lines
    if log_path:
        data["output_log_path"] = log_path
    return data


def build_terminal_started_display_data(*, command: str, workdir: Path) -> dict[str, Any]:
    """命令已拉起、还没有结论时的展示数据：只有命令与工作目录，没有输出与退出码。

    两处共用同一形状：前台命令启动时经实时通道交给卡片（输出块随后追加），后台占位写入的展示数据。
    「在后台」不写在这里：它是所有后台结果共有的事实，由占位行 metadata 的 ``background`` 投影到
    工具 part（持久化、结算时保留），前端统一读取，不按工具各存一份。卡片状态以 part 生命周期为准
    （占位为 ``pending``，结论并入或撤销时推到终态）。
    """

    return {"kind": "terminal-result", "command": command, "workdir": str(workdir)}


def build_terminal_list_display_data(terminal_count: int) -> dict[str, object]:
    """只展示本次发现数量；会话实时状态由 snapshot 的 terminal_sessions 提供。"""

    return {"kind": "terminal-session", "terminal_count": terminal_count}


def build_terminal_observation_display_data(
    result: TerminalObservationResult,
) -> dict[str, object]:
    """read / write 动作行：终端、填入命令摘要、等待结果与返回行范围；不承载终端输出。

    全屏程序期间返回的是整屏（``screen``），没有逻辑行区间。
    """

    data: dict[str, object] = {
        "kind": "terminal-session",
        "terminal_id": result.terminal_id,
        "lines": list(result.lines) if result.lines is not None else None,
        "screen": result.screen,
        "wait_result": dict(result.wait_result) if result.wait_result is not None else None,
    }
    if result.prefilled is not None:
        data["input_summary"] = result.prefilled
    return data
