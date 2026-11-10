"""execute_terminal shell 参数契约与本机显式 shell 执行测试。"""

import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tools.tool_handler.terminal.local_backend import (
    _Launch,
    _resolve_command,
)
from app.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    WindowsExecuteTerminalArgs,
    resolve_execute_terminal_args_model,
)
from tests.terminal_test_support import requires_terminal_worker, run_local_command


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
@requires_terminal_worker
def test_local_backend_runs_explicit_cmd(tmp_path) -> None:
    result, output = run_local_command(  # noqa: S604 - explicit shell under test
        "echo execute-terminal-cmd-smoke", tmp_path, 10, shell="cmd"
    )

    assert result.exit_code == 0
    assert "execute-terminal-cmd-smoke" in output.display


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is not available",
)
@requires_terminal_worker
def test_local_backend_runs_explicit_powershell(tmp_path) -> None:
    result, output = run_local_command(  # noqa: S604 - explicit shell under test
        "Write-Output execute-terminal-powershell-smoke", tmp_path, 10, shell="powershell"
    )

    assert result.exit_code == 0
    assert "execute-terminal-powershell-smoke" in output.display


@pytest.mark.skipif(os.name != "nt", reason="cmd 是 Windows 专属 shell")
def test_resolve_command_cmd_reuses_raw_command_string() -> None:
    """回归锁：Windows 的 cmd 与 auto 同为「命令原文」形态：``<COMSPEC> /c "<命令>"`` 按原文拼进
    命令行，不得退回参数形态。

    参数形态会按 MSVCRT 规则把命令内部引号转义成 cmd 无法还原的 ``\\"``，导致含引号命令（带
    空格路径等）报「文件名、目录名或卷标语法不正确」。
    """

    command = 'dir /a /s /b "C:\\Program Files"'
    comspec = os.environ.get("COMSPEC") or "cmd.exe"

    expected = _Launch(comspec, (f'/c "{command}"',), raw_args=True)
    assert _resolve_command(command, "auto") == expected
    assert _resolve_command(command, "cmd") == expected


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("powershell.exe") is None,
    reason="Windows PowerShell is not available",
)
def test_resolve_command_powershell_keeps_command_as_single_argv_element() -> None:
    """回归锁：PowerShell 分支保持 argv 单元素传参（改成拼接命令行串会让引号被吞掉）。"""

    command = 'Write-Output "a b"'
    launch = _resolve_command(command, "powershell")

    assert launch.raw_args is False
    assert launch.args == ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 显式 shell 分支")
def test_resolve_command_posix_keeps_command_as_single_argv_element() -> None:
    """回归锁：POSIX 显式 shell 走 ``<shell> -c <命令>``，命令作为单个 argv 元素传递。"""

    command = 'echo "a b"'

    assert _resolve_command(command, "sh") == _Launch("/bin/sh", ("-c", command))
    assert _resolve_command(command, "auto") == _Launch("/bin/sh", ("-c", command))


@pytest.mark.skipif(os.name != "nt", reason="cmd 引号命令是 Windows 专属回归")
@requires_terminal_worker
def test_local_backend_runs_explicit_cmd_with_quoted_path(tmp_path: Path) -> None:
    """回归：显式 cmd 下带引号的路径必须正常执行。

    历史缺陷：``shell="cmd"`` 走 argv 形态，``list2cmdline`` 把路径引号转义成 ``\\"``，
    cmd 收到 ``dir /b \\"...\\"`` 后报「文件名、目录名或卷标语法不正确」且退出码为 1。
    """

    target = tmp_path / "quoted dir"
    target.mkdir()
    (target / "marker.txt").write_text("x", encoding="utf-8")

    result, output = run_local_command(  # noqa: S604 - explicit shell under test
        f'dir /b "{target}"', tmp_path, 10, shell="cmd"
    )

    assert result.exit_code == 0
    assert "marker.txt" in output.display


@pytest.mark.skipif(os.name != "nt", reason="cmd 引号命令是 Windows 专属回归")
@requires_terminal_worker
def test_local_backend_runs_explicit_cmd_with_quoted_multiword_argument(tmp_path: Path) -> None:
    """回归：显式 cmd 下引号内的多词参数必须作为一个整体传给被调命令。

    历史缺陷：同一 argv 转义问题会让 ``findstr /I "alpha beta" <file>`` 把 ``alpha`` 与
    ``beta`` 当成两个文件路径（表现为 ``FINDSTR: Cannot open beta"``）。
    """

    target = tmp_path / "sample.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    result, output = run_local_command(  # noqa: S604 - explicit shell under test
        f'findstr /I "alpha beta" "{target}"', tmp_path, 10, shell="cmd"
    )

    assert result.exit_code == 0
    assert "alpha beta gamma" in output.display
    assert "Cannot open" not in output.display


@pytest.mark.skipif(os.name != "posix", reason="用 dup2 替换 fd 0 模拟宿主存活管道")
@requires_terminal_worker
def test_local_backend_does_not_inherit_backend_stdin(tmp_path: Path) -> None:
    """目的：命令拿到空 stdin 立即 EOF。潜在缺陷：后端 stdin 是宿主从不写入的存活管道，
    命令继承它后，读 stdin 的命令（如 ``cat``）会一直挂到超时。"""
    read_end, write_end = os.pipe()
    saved_stdin = os.dup(0)
    os.dup2(read_end, 0)
    try:
        result, _output = run_local_command("cat", tmp_path, 5)
    finally:
        os.dup2(saved_stdin, 0)
        for fd in (saved_stdin, read_end, write_end):
            os.close(fd)

    assert result.end == "exited"
    assert result.exit_code == 0
