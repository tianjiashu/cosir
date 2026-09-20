"""终端工具的 UI 展示数据构造。"""

from pathlib import Path
from typing import Any


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
