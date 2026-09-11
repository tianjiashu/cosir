"""execute_terminal shell 参数与本机显式 shell 执行测试。"""

import os
import shutil

import pytest
from pydantic import ValidationError

from app.core.tools.tool_handler.terminal.local_backend import LocalExecutionBackend
from app.core.tools.tool_models.execute_terminal_args import ExecuteTerminalArgs


def test_execute_terminal_args_defaults_to_auto_and_rejects_unknown_shell() -> None:
    args = ExecuteTerminalArgs(command="echo hello")

    assert args.shell == "auto"
    with pytest.raises(ValidationError):
        ExecuteTerminalArgs(command="echo hello", shell="unknown")  # noqa: S604


@pytest.mark.skipif(os.name != "nt", reason="cmd smoke test is Windows-specific")
def test_local_backend_runs_explicit_cmd(tmp_path) -> None:
    result = LocalExecutionBackend().execute(  # noqa: S604 - explicit shell under test
        "echo execute-terminal-cmd-smoke",
        str(tmp_path),
        10,
        shell="cmd",
    )

    assert result.exit_code == 0
    assert "execute-terminal-cmd-smoke" in result.output


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is not available",
)
def test_local_backend_runs_explicit_powershell(tmp_path) -> None:
    result = LocalExecutionBackend().execute(  # noqa: S604 - explicit shell under test
        "Write-Output execute-terminal-powershell-smoke",
        str(tmp_path),
        10,
        shell="powershell",
    )

    assert result.exit_code == 0
    assert "execute-terminal-powershell-smoke" in result.output
