"""用户命令环境：启动期合并登录 shell 环境，派生用户命令时去掉本应用变量。"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.tools.shell import login_environment
from app.tools.shell.command_environment import user_command_environment
from app.tools.shell.login_environment import RESOLVING_FLAG, apply_login_shell_environment
from app.tools.terminal_runtime import worker_client
from app.tools.terminal_runtime.errors import TerminalWorkerUnavailableError
from app.tools.terminal_runtime.shell_resolver import ShellSpec
from app.tools.tool_handler.terminal.local_backend import LocalExecutionBackend
from tests.terminal_test_support import requires_terminal_worker

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="登录 shell 解析只在 POSIX 上进行")


def _fake_shell(tmp_path: Path, body: str) -> str:
    """一个按 ``-i -l -c <script>`` 调用的假 shell：先执行 ``body``，再执行传入的脚本。"""
    shell = tmp_path / "fakesh"
    shell.write_text(f'#!/bin/sh\n{body}\neval "$4"\n')
    shell.chmod(shell.stat().st_mode | stat.S_IEXEC)
    return str(shell)


@posix_only
def test_login_environment_merges_profile_values_but_keeps_host_contract(tmp_path: Path) -> None:
    """目的：从 Dock 启动时补上用户 PATH；潜在缺陷：rc 改写宿主契约变量或把解析标记带进进程环境。"""

    shell = _fake_shell(
        tmp_path,
        "echo rc noise\n"
        "export PATH=/opt/user/bin:$PATH CODING_AGENT_PORT=9999\n"
        "export MULTI='line1\nline2'",
    )
    environ = {
        "SHELL": shell,
        "PATH": "/usr/bin:/bin",
        "CODING_AGENT_PORT": "1234",
        "HOME": str(tmp_path),
    }

    apply_login_shell_environment(environ)

    assert environ["PATH"].startswith("/opt/user/bin:")
    assert environ["MULTI"] == "line1\nline2"
    assert environ["CODING_AGENT_PORT"] == "1234"
    assert RESOLVING_FLAG not in environ
    assert "SHLVL" not in environ and "_" not in environ


@posix_only
def test_login_environment_falls_back_when_shell_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目的：rc 卡住时不阻断后端启动；潜在缺陷：超时后仍等待、或半截结果被合并。"""

    monkeypatch.setattr(login_environment, "_TIMEOUT_SECONDS", 0.5)
    shell = _fake_shell(tmp_path, "sleep 30")
    environ = {"SHELL": shell, "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    apply_login_shell_environment(environ)

    assert environ == {"SHELL": shell, "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}


@posix_only
def test_login_environment_falls_back_without_markers(tmp_path: Path) -> None:
    """目的：shell 不执行脚本（如 rc 里 exec 了别的程序）时保留原环境。"""

    shell = tmp_path / "fakesh"
    shell.write_text("#!/bin/sh\necho no environment here\n")
    shell.chmod(shell.stat().st_mode | stat.S_IEXEC)
    environ = {"SHELL": str(shell), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}

    apply_login_shell_environment(environ)

    assert environ["PATH"] == "/usr/bin:/bin"


def test_login_environment_is_skipped_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_environment, "is_windows", lambda: True)
    environ = {"SHELL": "/does/not/exist"}

    apply_login_shell_environment(environ)

    assert environ == {"SHELL": "/does/not/exist"}


def test_user_command_environment_drops_host_and_env_file_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目的：用户命令只看到用户自己的环境；潜在缺陷：宿主 token 与 .env 里的配置被子进程继承。"""

    monkeypatch.setenv("CODING_AGENT_HOST_TOKEN", "secret")
    monkeypatch.setenv("FROM_ENV_FILE", "file")
    monkeypatch.setenv("USER_OWN", "kept")
    monkeypatch.setattr(Settings, "_LOADED_ENV_VALUES", {"FROM_ENV_FILE": "file"})

    environment = user_command_environment()

    assert "CODING_AGENT_HOST_TOKEN" not in environment
    assert "FROM_ENV_FILE" not in environment
    assert environment["USER_OWN"] == "kept"


@posix_only
@requires_terminal_worker
def test_execute_terminal_command_does_not_inherit_host_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODING_AGENT_HOST_TOKEN", "secret")
    monkeypatch.setenv("USER_OWN", "kept")

    result = LocalExecutionBackend().execute("env", str(tmp_path), timeout=10)

    assert "CODING_AGENT_HOST_TOKEN" not in result.output
    assert "USER_OWN=kept" in result.output


def test_terminal_worker_spawns_with_user_command_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 把自己的环境原样交给 shell，所以派生 worker 时就必须去掉本应用变量。"""

    captured: dict[str, object] = {}

    def fake_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        captured.update(kwargs)
        raise OSError("not started in this test")

    monkeypatch.setenv("CODING_AGENT_TERMINAL_WORKER", str(tmp_path / "terminal-worker"))
    monkeypatch.setenv("CODING_AGENT_HOST_TOKEN", "secret")
    monkeypatch.setattr(worker_client.subprocess, "Popen", fake_popen)
    client = worker_client.ProcessTerminalWorker("instance")
    spec = ShellSpec(kind="sh", executable="/bin/sh", argv=("/bin/sh",), integration="off")

    with pytest.raises(TerminalWorkerUnavailableError):
        client.start(spec, str(tmp_path), lambda event: None)

    env = captured["env"]
    assert isinstance(env, dict)
    assert "CODING_AGENT_HOST_TOKEN" not in env
    assert env.get("PATH") == os.environ.get("PATH")
