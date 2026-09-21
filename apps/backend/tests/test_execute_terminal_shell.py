"""execute_terminal shell 参数契约与本机显式 shell 执行测试。"""

import os
import shutil

import pytest
from pydantic import ValidationError

from app.core.tools.tool_handler.terminal.local_backend import LocalExecutionBackend
from app.core.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    WindowsExecuteTerminalArgs,
    resolve_execute_terminal_args_model,
)


def test_windows_args_default_to_auto_and_reject_posix_shells() -> None:
    """Windows 参数模型默认 auto，并在校验层拒收 POSIX 专属 shell。"""

    args = WindowsExecuteTerminalArgs(command="echo hello")

    assert args.shell == "auto"
    with pytest.raises(ValidationError):
        WindowsExecuteTerminalArgs(command="echo hello", shell="zsh")  # noqa: S604


def test_macos_args_default_to_auto_and_reject_windows_shells() -> None:
    """macOS 参数模型默认 auto，并在校验层拒收 Windows 专属 shell。"""

    args = MacExecuteTerminalArgs(command="echo hello")

    assert args.shell == "auto"
    with pytest.raises(ValidationError):
        MacExecuteTerminalArgs(command="echo hello", shell="cmd")  # noqa: S604


def test_platform_resolver_selects_args_model_by_system_name() -> None:
    """平台选择入口按 platform.system() 形态选型，非 Windows 一律走 POSIX 参数模型。"""

    assert resolve_execute_terminal_args_model("Windows") is WindowsExecuteTerminalArgs
    assert resolve_execute_terminal_args_model(" windows ") is WindowsExecuteTerminalArgs
    assert resolve_execute_terminal_args_model("Darwin") is MacExecuteTerminalArgs
    assert resolve_execute_terminal_args_model("Linux") is MacExecuteTerminalArgs
    assert resolve_execute_terminal_args_model("unknown") is MacExecuteTerminalArgs


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
