"""execute_terminal 平台相关模型描述测试。"""

import pytest

from app.core.tools.tool_handler.execute_terminal import ExecuteTerminalTool


def _patch_windows_environment(monkeypatch: pytest.MonkeyPatch, *, powershell: bool) -> None:
    """为描述测试构造稳定的 Windows shell 环境。"""

    monkeypatch.setattr(
        "app.core.tools.tool_handler.execute_terminal.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    available = {
        "cmd.exe": "C:/Windows/System32/cmd.exe",
        "powershell.exe": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if powershell
        else None,
        "pwsh.exe": None,
    }
    monkeypatch.setattr(
        "app.core.tools.tool_handler.execute_terminal.shutil.which",
        lambda name: available.get(name),
    )


def test_windows_description_explains_cmd_default_and_explicit_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_environment(monkeypatch, powershell=True)
    description = ExecuteTerminalTool().description

    assert "cmd.exe" in description
    assert "PowerShell is not the default" in description
    assert "powershell.exe" in description
    assert "Do not mix cmd and PowerShell syntax" in description


def test_macos_description_explains_posix_shell_and_zsh_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.execute_terminal.platform.system",
        lambda: "Darwin",
    )
    description = ExecuteTerminalTool().description

    assert "/bin/sh" in description
    assert "interactive login zsh" in description
    assert "/bin/zsh" in description


def test_linux_description_explains_posix_shell_and_bash_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.tools.tool_handler.execute_terminal.platform.system",
        lambda: "Linux",
    )
    description = ExecuteTerminalTool().description

    assert "/bin/sh" in description
    assert "/bin/bash" in description


def test_tool_definition_uses_instance_platform_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_environment(monkeypatch, powershell=True)
    tool = ExecuteTerminalTool()
    definition = tool.to_definition()

    assert definition.description == tool.description


def test_tool_definition_exposes_only_detected_shells_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_environment(monkeypatch, powershell=False)
    definition = ExecuteTerminalTool().to_definition()

    model_definition = definition.to_model_tool_definition()
    shell_schema = model_definition["parameters"]["properties"]["shell"]

    assert shell_schema["enum"] == ["auto", "cmd"]
    assert "'auto', 'cmd'" in shell_schema["description"]
    assert "powershell" not in shell_schema["enum"]
