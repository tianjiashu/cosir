"""execute_terminal 平台参数契约的模型可见投影测试。

平台 shell 契约（本机有哪些 shell、命令该用什么语法）的唯一事实源是参数模型，因此断言集中
在 ``to_model_tool_definition()["parameters"]``：平台选型、枚举收敛、描述文案三者必须同源。
工具描述只承载平台无关的执行语义，不重复声明平台 shell 细节。
"""

import re
from typing import cast, get_args

import pytest
from pydantic import BaseModel

from app.core.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.core.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    MacExecuteTerminalShell,
    WindowsExecuteTerminalArgs,
    WindowsExecuteTerminalShell,
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOOL_MODULE = "app.core.tools.tool_handler.execute_terminal"


def _patch_windows_environment(monkeypatch: pytest.MonkeyPatch, *, powershell: bool) -> None:
    """为描述测试构造稳定的 Windows shell 环境。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Windows")
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    available = {
        "cmd.exe": "C:/Windows/System32/cmd.exe",
        "powershell.exe": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        if powershell
        else None,
        "pwsh.exe": None,
    }
    monkeypatch.setattr(
        f"{_TOOL_MODULE}.shutil.which",
        lambda name: available.get(name),
    )


def _patch_macos_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """为描述测试构造稳定的 macOS shell 环境（只伪造 PATH 命中，不伪造 /bin/sh 探测）。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Darwin")
    available = {"bash": "/bin/bash", "zsh": "/bin/zsh", "fish": None, "pwsh": None}
    monkeypatch.setattr(f"{_TOOL_MODULE}.shutil.which", lambda name: available.get(name))


def _model_properties(tool: ExecuteTerminalTool) -> dict[str, dict[str, object]]:
    """返回该工具定义投影给模型的参数属性字典。"""

    definition = tool.to_definition()
    parameters = definition.to_model_tool_definition()["parameters"]
    return cast("dict[str, dict[str, object]]", parameters["properties"])


def test_windows_host_selects_windows_args_model_and_narrows_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_environment(monkeypatch, powershell=False)
    tool = ExecuteTerminalTool()
    shell_schema = _model_properties(tool)["shell"]

    assert tool.args_model is WindowsExecuteTerminalArgs
    assert shell_schema["enum"] == ["auto", "cmd"]
    description = str(shell_schema["description"])
    assert "cmd.exe" in description
    assert description.endswith("Available on this host: 'auto', 'cmd'.")


def test_macos_host_selects_macos_args_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_macos_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    shell_schema = _model_properties(tool)["shell"]
    enum = list(shell_schema["enum"])

    assert tool.args_model is MacExecuteTerminalArgs
    assert {"auto", "bash", "zsh"} <= set(enum)
    assert "cmd" not in enum
    assert "pwsh" not in enum
    description = str(shell_schema["description"])
    assert "/bin/sh" in description
    assert "interactive login zsh" in description
    assert "Available on this host:" in description


def test_tool_description_is_platform_neutral_and_points_to_shell_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_windows_environment(monkeypatch, powershell=True)
    windows_description = ExecuteTerminalTool().description
    _patch_macos_environment(monkeypatch)
    macos_description = ExecuteTerminalTool().description

    assert windows_description == macos_description
    assert "shell parameter description" in windows_description
    assert "cmd.exe" not in windows_description
    assert "/bin/sh" not in windows_description


@pytest.mark.parametrize(
    ("model", "expected_snippets"),
    [
        (WindowsExecuteTerminalArgs, ("cmd.exe", "PowerShell", "Get-ChildItem", "$env:VAR")),
        (MacExecuteTerminalArgs, ("/bin/sh", "interactive login zsh", "bash [[ ]]", "fish")),
    ],
)
def test_platform_shell_descriptions_carry_host_specific_syntax(
    model: type[BaseModel],
    expected_snippets: tuple[str, ...],
) -> None:
    """平台参数模型的静态 shell 描述必须给出该平台的语法与取值指引。"""

    description = str(model.model_json_schema()["properties"]["shell"]["description"])

    for snippet in expected_snippets:
        assert snippet in description


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_platform_args_descriptions_are_english_complete_and_non_redundant(
    model: type[BaseModel],
) -> None:
    """四个字段都必须有英文完整描述，且不存在重复或跨字段复述的描述文本。"""

    properties = model.model_json_schema()["properties"]

    assert set(properties) == {"command", "shell", "timeout", "workdir"}
    for name, schema in properties.items():
        description = str(schema["description"])
        assert description == description.strip(), name
        assert description.endswith("."), name
        assert len(description) >= 60, name  # 每个字段都要有实质说明，不接受一句话占位
        assert _CJK_RE.search(description) is None, name

    descriptions = [str(schema["description"]) for schema in properties.values()]
    for description in descriptions:
        assert sum(description in other for other in descriptions) == 1


def test_platform_args_descriptions_do_not_leak_the_other_platform_shell() -> None:
    """平台文案只讲本平台的 shell 语义，不得把另一平台的执行契约写进描述。"""

    windows_text = " ".join(
        str(schema["description"])
        for schema in WindowsExecuteTerminalArgs.model_json_schema()["properties"].values()
    )
    macos_text = " ".join(
        str(schema["description"])
        for schema in MacExecuteTerminalArgs.model_json_schema()["properties"].values()
    )

    for leaked in ("/bin/sh", "zsh", "bash"):
        assert leaked not in windows_text, leaked
    assert "cmd.exe" not in macos_text
    assert "cmd syntax" not in macos_text


def test_detected_shells_stay_inside_platform_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机探测出的 shell 必须落在平台参数模型的取值集合内（契约与探测不得漂移）。"""

    _patch_windows_environment(monkeypatch, powershell=True)
    windows_enum = set(_model_properties(ExecuteTerminalTool())["shell"]["enum"])
    _patch_macos_environment(monkeypatch)
    macos_enum = set(_model_properties(ExecuteTerminalTool())["shell"]["enum"])

    assert windows_enum <= {"auto", *get_args(WindowsExecuteTerminalShell)}
    assert macos_enum <= {"auto", *get_args(MacExecuteTerminalShell)}


def test_detected_shells_are_derived_from_the_platform_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测候选取自契约：全部视为已安装时，enum 必须等于契约取值的完整投影（含顺序）。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Windows")
    monkeypatch.setattr(f"{_TOOL_MODULE}.shutil.which", lambda name: f"C:/fake/{name}")
    enum = list(_model_properties(ExecuteTerminalTool())["shell"]["enum"])

    assert enum == list(get_args(WindowsExecuteTerminalShell))


def test_platform_name_casing_keeps_model_and_detection_on_the_same_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台名大小写变体下，选中的参数模型与探测分支必须同平台，不得出现契约/枚举分叉。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "windows")
    monkeypatch.setattr(
        f"{_TOOL_MODULE}.shutil.which",
        lambda name: "C:/Windows/System32/cmd.exe" if name == "cmd.exe" else None,
    )
    tool = ExecuteTerminalTool()
    enum = set(_model_properties(tool)["shell"]["enum"])

    assert tool.args_model is WindowsExecuteTerminalArgs
    assert enum == {"auto", "cmd"}
    assert enum <= {"auto", *get_args(WindowsExecuteTerminalShell)}


def test_execute_terminal_tool_name_is_platform_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拆分参数模型后工具名仍是 execute_terminal，平台差异只体现在参数契约上。"""

    _patch_windows_environment(monkeypatch, powershell=True)
    windows_definition = ExecuteTerminalTool().to_definition()
    _patch_macos_environment(monkeypatch)
    macos_definition = ExecuteTerminalTool().to_definition()

    assert windows_definition.name == "execute_terminal"
    assert macos_definition.name == "execute_terminal"
    assert windows_definition.args_model is not macos_definition.args_model
