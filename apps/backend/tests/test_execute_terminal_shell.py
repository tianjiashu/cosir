"""execute_terminal shell 参数契约与本机显式 shell 执行测试。"""

import os
import shutil
from pathlib import Path

import pytest
from app.core.tools.tool_handler.terminal.local_backend import (
    LocalExecutionBackend,
    _resolve_command,
)
from app.core.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    WindowsExecuteTerminalArgs,
    resolve_execute_terminal_args_model,
)
from pydantic import ValidationError


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


@pytest.mark.skipif(os.name != "nt", reason="cmd 是 Windows 专属 shell")
def test_resolve_command_cmd_reuses_raw_command_string() -> None:
    """回归锁：Windows 的 cmd 与 auto 同返回「命令原文字符串」，不得退回 argv 传参。

    argv 形态会让 ``list2cmdline`` 把命令内部引号转义成 cmd 无法还原的 ``\\"``，导致含引号
    命令（带空格路径等）报「文件名、目录名或卷标语法不正确」。
    """

    command = 'dir /a /s /b "C:\\Program Files"'

    assert _resolve_command(command, "auto") == (command, True)
    assert _resolve_command(command, "cmd") == (command, True)


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is not available",
)
def test_resolve_command_powershell_keeps_command_as_single_argv_element() -> None:
    """回归锁：PowerShell 分支保持 argv 单元素传参（改成拼接命令行串会让引号被吞掉）。"""

    command = 'Write-Output "a b"'
    argv, use_shell = _resolve_command(command, "powershell")

    assert use_shell is False
    assert isinstance(argv, list)
    assert argv[-1] == command
    assert argv[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 显式 shell 分支")
def test_resolve_command_posix_keeps_command_as_single_argv_element() -> None:
    """回归锁：POSIX 显式 shell 走 ``<shell> -c <命令>``，命令作为单个 argv 元素传递。"""

    command = 'echo "a b"'

    assert _resolve_command(command, "sh") == (["/bin/sh", "-c", command], False)
    assert _resolve_command(command, "auto") == (command, True)


@pytest.mark.skipif(os.name != "nt", reason="cmd 引号命令是 Windows 专属回归")
def test_local_backend_runs_explicit_cmd_with_quoted_path(tmp_path: Path) -> None:
    """回归：显式 cmd 下带引号的路径必须正常执行。

    历史缺陷：``shell="cmd"`` 走 argv 形态，``list2cmdline`` 把路径引号转义成 ``\\"``，
    cmd 收到 ``dir /b \\"...\\"`` 后报「文件名、目录名或卷标语法不正确」且退出码为 1。
    """

    target = tmp_path / "quoted dir"
    target.mkdir()
    (target / "marker.txt").write_text("x", encoding="utf-8")

    result = LocalExecutionBackend().execute(  # noqa: S604 - explicit shell under test
        f'dir /b "{target}"',
        str(tmp_path),
        10,
        shell="cmd",
    )

    assert result.exit_code == 0
    assert "marker.txt" in result.output


@pytest.mark.skipif(os.name != "nt", reason="cmd 引号命令是 Windows 专属回归")
def test_local_backend_runs_explicit_cmd_with_quoted_multiword_argument(tmp_path: Path) -> None:
    """回归：显式 cmd 下引号内的多词参数必须作为一个整体传给被调命令。

    历史缺陷：同一 argv 转义问题会让 ``findstr /I "alpha beta" <file>`` 把 ``alpha`` 与
    ``beta`` 当成两个文件路径（表现为 ``FINDSTR: Cannot open beta"``）。
    """

    target = tmp_path / "sample.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    result = LocalExecutionBackend().execute(  # noqa: S604 - explicit shell under test
        f'findstr /I "alpha beta" "{target}"',
        str(tmp_path),
        10,
        shell="cmd",
    )

    assert result.exit_code == 0
    assert "alpha beta gamma" in result.output
    assert "Cannot open" not in result.output
