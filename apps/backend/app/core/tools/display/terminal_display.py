"""终端工具的 UI 展示数据构造。"""

from pathlib import Path
from typing import Any

from app.utils.trace_infra.redaction import redact_terminal_output


def build_terminal_display_data(
    *,
    command: str,
    workdir: Path,
    output: str,
    exit_code: int,
    timed_out: bool,
    truncated: bool,
) -> dict[str, Any]:
    """构造脱敏且有界的终端展示数据。

    命令和输出都经过自由文本凭据脱敏；本函数不执行命令，也不负责工具状态判断。
    """

    return {
        "kind": "terminal-result",
        "command": redact_terminal_output(command),
        "workdir": str(workdir),
        "output": redact_terminal_output(output),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": truncated,
    }
