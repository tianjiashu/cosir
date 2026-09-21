"""execute_terminal 平台参数契约的对抗性验证（独立测试，不修改产品代码）。

覆盖点（逐条对应需求）：
1. ``resolve_execute_terminal_args_model`` 的平台名分支（含大小写 / 首尾空白 / 空串 / 未知）。
2. 端到端校验：``to_definition()`` → ``validate_tool_arguments`` 的三条路径
   （合法省略 shell、跨平台 shell 被拒、未知字段被拒、timeout=0 被拒），
   并断言归一化后的键集合与 ``execute`` 形参一致（键错位会导致子进程调用 TypeError）。
3. schema 与模型一致性：模型可见 ``enum`` ⊆ 平台 ``Literal``；描述里
   "Available on this host:" 的值与 ``enum`` 完全一致（含顺序）。
4. 描述质量：非空 / 无 CJK / 句号结尾 / 类内互不包含 / 不泄漏对端平台字面量。
5. 工具名不变 + ``ToolRegistry`` 同名去重只保留一个定义。
6. 进程隔离兼容：pickle 往返后 ``args_model`` 仍指向正确平台类。

所有断言都指向「契约」而非实现细节；发现缺陷只报告不修复。
"""

import inspect
import pickle
import re
from pathlib import Path
from typing import Any, cast, get_args

import pytest
from pydantic import BaseModel

from app.core.tools.schemas import ToolDefinition
from app.core.tools.tool_handler.execute_terminal import ExecuteTerminalTool
from app.core.tools.tool_models.execute_terminal_args import (
    MacExecuteTerminalArgs,
    MacExecuteTerminalShell,
    WindowsExecuteTerminalArgs,
    WindowsExecuteTerminalShell,
    resolve_execute_terminal_args_model,
)
from app.core.tools.tool_registry import ToolRegistry
from app.core.tools.validation.arguments import validate_tool_arguments

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOOL_MODULE = "app.core.tools.tool_handler.execute_terminal"

# execute 的形参必须是「args_model 字段 + 执行链注入的 execution_context / output_sink」。
_EXECUTION_INJECTED_PARAMS = {"execution_context", "output_sink"}


# ----------------------------------------------------------------------
# 环境夹具：伪造平台与可用 shell，避免依赖运行机实际环境
# ----------------------------------------------------------------------


def _patch_windows_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cmd: bool = True,
    powershell: bool = True,
    pwsh: bool = True,
) -> None:
    """构造稳定的 Windows shell 环境（只伪造 PATH 命中与环境变量）。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Windows")
    if cmd:
        monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    else:
        monkeypatch.delenv("COMSPEC", raising=False)
    available = {
        "cmd.exe": "C:/Windows/System32/cmd.exe" if cmd else None,
        "powershell.exe": "C:/Windows/System32/powershell.exe" if powershell else None,
        "pwsh.exe": "C:/Program Files/PowerShell/pwsh.exe" if pwsh else None,
    }
    monkeypatch.setattr(f"{_TOOL_MODULE}.shutil.which", lambda name: available.get(name))


class _FakePath:
    """替身 ``pathlib.Path``：只实现 handler 探测 ``/bin/sh`` 所需的最小接口。

    既是替换用的「类」（``_FakePath(bin_sh_exists=...)``），也是可调用的构造器
    （``_FakePath("/bin/sh")`` 返回自身），两次调用都代表同一个查询结果。
    """

    def __init__(self, *_args: Any, bin_sh_exists: bool = True, **_kwargs: Any) -> None:
        self._bin_sh_exists = bin_sh_exists

    def __call__(self, *_args: Any, **_kwargs: Any) -> "_FakePath":
        return self

    def is_file(self) -> bool:
        return self._bin_sh_exists

    def __repr__(self) -> str:
        return "<FakePath>"


def _patch_macos_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bin_sh: bool = True,
    extra: tuple[str, ...] = ("bash", "zsh"),
) -> None:
    """构造稳定的 macOS shell 环境（只伪造 PATH 命中与 /bin/sh 探测）。"""

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Darwin")
    available = {name: f"/usr/bin/{name}" for name in extra}
    for name in ("bash", "zsh", "fish", "pwsh"):
        available.setdefault(name, None)
    monkeypatch.setattr(f"{_TOOL_MODULE}.shutil.which", lambda name: available.get(name))
    monkeypatch.setattr(f"{_TOOL_MODULE}.Path", _FakePath(bin_sh_exists=bin_sh))
    monkeypatch.setattr(f"{_TOOL_MODULE}.os.access", lambda *_a, **_k: bin_sh)


def _model_shell_schema(tool: ExecuteTerminalTool) -> dict[str, Any]:
    """返回该工具投影给模型的 shell 属性 schema。"""

    definition = tool.to_definition()
    parameters = definition.to_model_tool_definition()["parameters"]
    properties = cast("dict[str, Any]", parameters["properties"])
    return cast("dict[str, Any]", properties["shell"])


def _enum_values(shell_schema: dict[str, Any]) -> list[str]:
    """把 schema 里的 enum 取值归一化为字符串列表。"""

    return [str(value) for value in shell_schema["enum"]]


def _availability_section(description: str) -> str:
    """截取描述里 "Available on this host:" 之后的一段。"""

    marker = "Available on this host:"
    assert marker in description, f"缺少本机可用值说明: {description!r}"
    return description.split(marker, 1)[1]


def _availability_values(description: str) -> list[str]:
    """从描述里解析出 "Available on this host:" 列出的取值（保序）。"""

    section = _availability_section(description).strip()
    if section.endswith("."):
        section = section[:-1]
    return [chunk.strip().strip("'") for chunk in section.split(",") if chunk.strip()]


# ----------------------------------------------------------------------
# 1. 平台选择入口
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "system_name",
    ["Windows", " windows ", "WINDOWS", "Windows\t", "\twindows\n", "WiNdOwS"],
)
def test_resolver_returns_windows_model_for_windows_variants(system_name: str) -> None:
    """平台名容错（大小写 + 首尾空白）必须都命中 Windows 模型，否则会漏用 Windows 契约。"""

    assert resolve_execute_terminal_args_model(system_name) is WindowsExecuteTerminalArgs


@pytest.mark.parametrize(
    "system_name",
    ["Darwin", "Linux", "", "unknown", "windows7", "Windows NT", "iOS", "freebsd"],
)
def test_resolver_falls_back_to_mac_model_for_non_windows(system_name: str) -> None:
    """非 Windows（含空串与未知值）一律走 POSIX 契约；误判会放行 Windows 专属 shell。"""

    assert resolve_execute_terminal_args_model(system_name) is MacExecuteTerminalArgs


def test_resolver_never_returns_a_shared_base_of_both_models() -> None:
    """两个平台类必须彼此独立（无共同基类），否则平台契约无法真正收口。"""

    assert issubclass(WindowsExecuteTerminalArgs, BaseModel)
    assert issubclass(MacExecuteTerminalArgs, BaseModel)
    assert not issubclass(WindowsExecuteTerminalArgs, MacExecuteTerminalArgs)
    assert not issubclass(MacExecuteTerminalArgs, WindowsExecuteTerminalArgs)
    shared_bases = set(WindowsExecuteTerminalArgs.__mro__) & set(MacExecuteTerminalArgs.__mro__)
    assert shared_bases == {BaseModel, object}


# ----------------------------------------------------------------------
# 2. 端到端校验
# ----------------------------------------------------------------------


def _execute_param_without_defaults() -> set[str]:
    """返回 ``ExecuteTerminalTool.execute`` 中不接收注入项的形参名集合。"""

    signature = inspect.signature(ExecuteTerminalTool.execute)
    return {
        name
        for name, param in signature.parameters.items()
        if name not in _EXECUTION_INJECTED_PARAMS
        and name != "self"
        and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


@pytest.mark.parametrize("platform_name", ["Windows", "Darwin"])
def test_valid_arguments_without_shell_pass_and_match_execute_signature(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    """省略 shell 的合法参数必须通过，且归一化键集合 == execute 形参（键错位=子进程 TypeError）。"""

    if platform_name == "Windows":
        _patch_windows_environment(monkeypatch)
    else:
        _patch_macos_environment(monkeypatch)

    definition = ExecuteTerminalTool().to_definition()
    result = validate_tool_arguments(
        {"command": "echo hi"},
        definition.parameters_schema,
        definition.args_model,
    )

    assert result.ok is True, result.error
    assert result.arguments["command"] == "echo hi"
    assert result.arguments["shell"] == "auto"
    assert result.arguments["timeout"] is None
    assert result.arguments["workdir"] is None
    assert set(result.arguments.keys()) == _execute_param_without_defaults()


def test_windows_rejects_posix_shell_through_validation_gate() -> None:
    """Windows 上 shell='zsh' 必须被门禁拒收（否则下发必然失败的调用）。"""

    result = validate_tool_arguments(
        {"command": "echo hi", "shell": "zsh"},
        {},
        WindowsExecuteTerminalArgs,
    )
    assert result.ok is False
    assert "shell" in result.error


def test_macos_rejects_windows_shell_through_validation_gate() -> None:
    """macOS 上 shell='cmd' 必须被门禁拒收。"""

    result = validate_tool_arguments(
        {"command": "echo hi", "shell": "cmd"},
        {},
        MacExecuteTerminalArgs,
    )
    assert result.ok is False
    assert "shell" in result.error


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_validation_rejects_unknown_field(model: type[BaseModel]) -> None:
    """extra='forbid'：未知字段必须被拒，否则拼写错误的参数会被静默丢弃。"""

    result = validate_tool_arguments(
        {"command": "echo hi", "shelll": "auto"},
        {},
        model,
    )
    assert result.ok is False
    assert "shelll" in result.error


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_validation_rejects_zero_and_negative_timeout(model: type[BaseModel]) -> None:
    """timeout 必须严格 > 0；0 或负数意味着立即强杀，不能放行。"""

    for bad in (0, -1):
        result = validate_tool_arguments({"command": "echo hi", "timeout": bad}, {}, model)
        assert result.ok is False, f"timeout={bad} 未被拒收"
        assert "timeout" in result.error


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_validation_rejects_empty_command_and_non_object_payload(
    model: type[BaseModel],
) -> None:
    """command 的 min_length=1 与非对象入参都必须被拒，不得静默放行空命令。"""

    empty = validate_tool_arguments({"command": ""}, {}, model)
    assert empty.ok is False
    assert "command" in empty.error

    not_object = validate_tool_arguments(["echo hi"], {}, model)
    assert not_object.ok is False


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_validation_is_strict_about_shell_subtype(model: type[BaseModel]) -> None:
    """strict 模型不得把非字符串（如 None/True）强转为合法 shell 值。"""

    for bad in (None, True, 0):
        result = validate_tool_arguments({"command": "echo hi", "shell": bad}, {}, model)
        assert result.ok is False, f"shell={bad!r} 被错误放行"


def test_validation_reports_all_issues_at_once() -> None:
    """错误信息应聚合多字段问题，便于模型一次修正（门禁契约）。"""

    result = validate_tool_arguments(
        {"shell": "zsh", "timeout": 0},
        {},
        WindowsExecuteTerminalArgs,
    )
    assert result.ok is False
    # command 缺失 + shell 非法 + timeout 非法 = 3 个问题，必须一次性全部回报
    assert "3 issue(s)" in result.error


# ----------------------------------------------------------------------
# 3. schema 与模型一致性
# ----------------------------------------------------------------------


def test_windows_schema_enum_is_subset_of_literal_and_matches_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows：enum ⊆ Literal，且描述里的可用值与 enum 同集合同顺序。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    assert tool.args_model is WindowsExecuteTerminalArgs
    shell_schema = _model_shell_schema(tool)
    enum = _enum_values(shell_schema)

    assert enum[0] == "auto", "auto 必须恒在首位，否则提示顺序不稳定"
    assert set(enum) <= {"auto", *get_args(WindowsExecuteTerminalShell)}
    assert _availability_values(str(shell_schema["description"])) == enum


def test_macos_schema_enum_is_subset_of_literal_and_matches_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS：enum ⊆ Literal，且描述里的可用值与 enum 同集合同顺序。"""

    _patch_macos_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    assert tool.args_model is MacExecuteTerminalArgs
    shell_schema = _model_shell_schema(tool)
    enum = _enum_values(shell_schema)

    assert enum[0] == "auto"
    assert set(enum) <= {"auto", *get_args(MacExecuteTerminalShell)}
    assert "cmd" not in enum
    assert _availability_values(str(shell_schema["description"])) == enum


def test_windows_enum_tracks_actual_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅装 cmd 时 enum 只能是 ['auto','cmd']；收窄必须跟随实测而非硬编码。"""

    _patch_windows_environment(monkeypatch, cmd=True, powershell=False, pwsh=False)
    shell_schema = _model_shell_schema(ExecuteTerminalTool())
    assert _enum_values(shell_schema) == ["auto", "cmd"]


def test_enum_reverts_to_auto_only_when_nothing_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个显式 shell 都检测不到时，enum 必须退化为 ['auto'] 且描述仍与之同源。"""

    _patch_windows_environment(monkeypatch, cmd=False, powershell=False, pwsh=False)
    shell_schema = _model_shell_schema(ExecuteTerminalTool())
    enum = _enum_values(shell_schema)
    assert enum == ["auto"]
    assert _availability_values(str(shell_schema["description"])) == ["auto"]


def test_schema_does_not_widen_model_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型可见 schema 只能收窄不能放宽：不得出现 Literal 之外的值（含 pwsh 越界）。"""

    _patch_windows_environment(monkeypatch, cmd=False, powershell=False, pwsh=True)
    enum = set(_enum_values(_model_shell_schema(ExecuteTerminalTool())))
    assert enum <= {"auto", *get_args(WindowsExecuteTerminalShell)}


# ----------------------------------------------------------------------
# 4. 描述质量
# ----------------------------------------------------------------------


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_all_four_field_descriptions_are_nonempty_english_and_stop_with_period(
    model: type[BaseModel],
) -> None:
    """四字段描述必须非空、英文、无 CJK、无首尾空白、以句号结尾。"""

    properties = model.model_json_schema()["properties"]
    assert set(properties) == {"shell", "command", "timeout", "workdir"}
    for name, schema in properties.items():
        description = str(schema["description"])
        assert description.strip(), f"{model.__name__}.{name} 描述为空"
        assert description == description.strip(), f"{model.__name__}.{name} 首尾有空白"
        assert description.endswith("."), f"{model.__name__}.{name} 未以句号结尾"
        assert _CJK_RE.search(description) is None, f"{model.__name__}.{name} 含中文"


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_descriptions_are_mutually_non_contained_within_a_model(
    model: type[BaseModel],
) -> None:
    """类内四个字段描述互不包含，避免复制粘贴造成的冗余契约。"""

    descriptions = [
        str(schema["description"]) for schema in model.model_json_schema()["properties"].values()
    ]
    for index, description in enumerate(descriptions):
        for other_index, other in enumerate(descriptions):
            if index == other_index:
                continue
            assert description not in other, f"{model.__name__} 描述冗余：{description[:40]}"


def test_windows_descriptions_do_not_leak_posix_shells() -> None:
    """Windows 类描述不得出现 /bin/sh、zsh、bash（会诱导模型用不存在的 shell）。"""

    text = " ".join(
        str(schema["description"])
        for schema in WindowsExecuteTerminalArgs.model_json_schema()["properties"].values()
    ).lower()
    for leaked in ("/bin/sh", "zsh", "bash"):
        assert leaked not in text, f"Windows 描述泄漏 POSIX 契约: {leaked}"


def test_macos_descriptions_do_not_leak_windows_shells() -> None:
    """macOS 类描述不得出现 cmd.exe、cmd syntax。"""

    text = " ".join(
        str(schema["description"])
        for schema in MacExecuteTerminalArgs.model_json_schema()["properties"].values()
    ).lower()
    assert "cmd.exe" not in text
    assert "cmd syntax" not in text


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_descriptions_are_absent_of_trailing_whitespace_artifacts(
    model: type[BaseModel],
) -> None:
    """描述不得以空格拼接缺陷留下 "  Available on this host" 之类的双空格。"""

    for name, schema in model.model_json_schema()["properties"].items():
        description = str(schema["description"])
        assert "  " not in description, f"{model.__name__}.{name} 存在连续空格"


def test_appended_availability_has_no_leading_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """本机可用值段落与静态描述之间必须恰有一个空格，不能出现双空格或缺失。"""

    _patch_windows_environment(monkeypatch)
    description = str(_model_shell_schema(ExecuteTerminalTool())["description"])
    assert "  " not in description
    assert "host:  " not in description


# ----------------------------------------------------------------------
# 5. 工具名 + 注册去重
# ----------------------------------------------------------------------


@pytest.mark.parametrize("platform_name", ["Windows", "Darwin"])
def test_tool_name_is_execute_terminal_on_every_host(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    """两种宿主下工具名必须都是 execute_terminal（不得产生第二个工具）。"""

    if platform_name == "Windows":
        _patch_windows_environment(monkeypatch)
    else:
        _patch_macos_environment(monkeypatch)

    definition = ExecuteTerminalTool().to_definition()
    assert definition.name == "execute_terminal"
    assert definition.handler.__self__.name == "execute_terminal"


def test_registry_skips_duplicate_name_and_keeps_single_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个平台实例先后注册时，第二个同名注册必须被跳过，注册表只保留一个定义。"""

    _patch_windows_environment(monkeypatch)
    windows_definition = ExecuteTerminalTool().to_definition()
    _patch_macos_environment(monkeypatch)
    macos_definition = ExecuteTerminalTool().to_definition()

    assert windows_definition.name == macos_definition.name == "execute_terminal"
    assert windows_definition.args_model is not macos_definition.args_model

    registry = ToolRegistry()
    registry.register(windows_definition)
    generation_after_first = registry.generation
    registry.register(macos_definition)

    assert registry.get_all_tool_names() == ["execute_terminal"]
    assert len(registry.get_all_definitions()) == 1
    assert registry.generation == generation_after_first, "重复注册不应改变 generation"
    kept = registry.get_tool_definition("execute_terminal")
    assert kept is not None
    assert kept.args_model is WindowsExecuteTerminalArgs


# ----------------------------------------------------------------------
# 6. 进程隔离兼容（pickle 往返）
# ----------------------------------------------------------------------


def test_definition_execution_mode_is_process() -> None:
    """execute_terminal 必须声明 process 隔离（跑任意 shell 需树杀兜底）。"""

    definition = ExecuteTerminalTool().to_definition()
    assert definition.execution_mode == "process"


@pytest.mark.parametrize("platform_name", ["Windows", "Darwin"])
def test_tool_instance_pickle_roundtrip_keeps_platform_args_model(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
) -> None:
    """pickle 往返后 args_model 仍指向正确的平台类，且可用 shell 集合不变。

    子进程以 spawn 方式启动时要重新导入 handler；若平台类在往返中退化，
    子进程会用错契约校验参数。
    """

    if platform_name == "Windows":
        _patch_windows_environment(monkeypatch)
        expected_model: type[BaseModel] = WindowsExecuteTerminalArgs
    else:
        _patch_macos_environment(monkeypatch)
        expected_model = MacExecuteTerminalArgs

    tool = ExecuteTerminalTool()
    restored = pickle.loads(pickle.dumps(tool))  # noqa: S301 - 测试内固定数据

    assert restored.args_model is expected_model
    assert restored.args_model is tool.args_model
    assert restored._available_shells == tool._available_shells
    assert restored._platform_name == tool._platform_name


def test_definition_pickle_roundtrip_keeps_name_and_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨进程传递的是 ToolDefinition；往返后名称/schema/args_model 必须一致。"""

    _patch_windows_environment(monkeypatch)
    definition = ExecuteTerminalTool().to_definition()
    restored: ToolDefinition = pickle.loads(pickle.dumps(definition))  # noqa: S301

    assert restored.name == definition.name == "execute_terminal"
    assert restored.args_model is definition.args_model
    assert dict(restored.parameters_schema) == dict(definition.parameters_schema)
    assert restored.execution_mode == "process"


def test_narrowed_schema_survives_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """registry 注册会对定义做 normalized()；本机收敛的 enum 不得被重新放宽。"""

    _patch_windows_environment(monkeypatch, cmd=True, powershell=False, pwsh=False)
    definition = ExecuteTerminalTool().to_definition()
    normalized = definition.normalized()
    normalized_enum = normalized.parameters_schema["properties"]["shell"]["enum"]  # type: ignore[index]
    assert list(normalized_enum) == ["auto", "cmd"]


# ----------------------------------------------------------------------
# 7. 探测与描述装配的边界（对抗性补充）
# ----------------------------------------------------------------------


def test_windows_comspec_alone_is_enough_for_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 上即使 cmd.exe 不在 PATH，只要 COMSPEC 存在就应认定 cmd 可用。

    过度收窄会让可选值凭空消失，属于本机收敛逻辑的边界缺陷。
    """

    monkeypatch.setattr(f"{_TOOL_MODULE}.platform.system", lambda: "Windows")
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    monkeypatch.setattr(f"{_TOOL_MODULE}.shutil.which", lambda name: None)
    tool = ExecuteTerminalTool()
    assert "cmd" in tool._available_shells
    assert _model_shell_schema(tool)["enum"] == ["auto", "cmd"]


def test_macos_detection_preserves_declared_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX 分支的 enum 顺序必须是 auto → sh → bash → zsh → fish → pwsh（稳定可复现）。"""

    _patch_macos_environment(monkeypatch, bin_sh=True, extra=("bash", "zsh", "fish", "pwsh"))
    enum = _model_shell_schema(ExecuteTerminalTool())["enum"]
    assert enum == ["auto", "sh", "bash", "zsh", "fish", "pwsh"]


def test_macos_without_bin_sh_still_reports_detected_shells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/bin/sh 不存在时不应崩溃，且只能列出实测命中的 shell。"""

    _patch_macos_environment(monkeypatch, bin_sh=False, extra=("bash",))
    enum = _model_shell_schema(ExecuteTerminalTool())["enum"]
    assert enum == ["auto", "bash"]


def test_schema_enum_order_is_stable_across_repeated_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一实例重复投影必须给出完全一致的 enum 顺序（否则模型侧 schema 抖动）。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    first = _enum_values(_model_shell_schema(tool))
    for _ in range(3):
        assert _enum_values(_model_shell_schema(tool)) == first


def test_two_shells_appended_to_description_are_quoted_consistently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """描述里的取值必须带引号且无多余空格，保证与 enum 逐项可解析对比。"""

    _patch_macos_environment(monkeypatch, bin_sh=True, extra=("bash",))
    description = str(_model_shell_schema(ExecuteTerminalTool())["description"])
    section = _availability_section(description)
    assert section.strip() == "'auto', 'sh', 'bash'."


def test_schema_projection_does_not_mutate_the_args_model_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机收敛只改投影副本，不得写回 args_model 的静态 schema（否则跨测试污染）。"""

    _patch_windows_environment(monkeypatch, cmd=True, powershell=False, pwsh=False)
    tool = ExecuteTerminalTool()
    _model_shell_schema(tool)

    static_schema = WindowsExecuteTerminalArgs.model_json_schema()
    assert static_schema["properties"]["shell"]["enum"] == list(
        get_args(WindowsExecuteTerminalShell)
    )
    assert "Available on this host:" not in static_schema["properties"]["shell"]["description"]


def test_each_cached_property_lookup_returns_fresh_schema_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两次 to_definition() 得到的 enum 互不共享同一 list 对象（避免外部就地改动回写）。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    first = _model_shell_schema(tool)["enum"]
    second = _model_shell_schema(tool)["enum"]
    assert first == second
    assert first is not second


def test_resolver_is_case_and_whitespace_tolerant_for_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 Windows 分支同样要容错大小写/空白，避免误判为 Windows。"""

    assert resolve_execute_terminal_args_model("  DARWIN  ") is MacExecuteTerminalArgs
    assert resolve_execute_terminal_args_model("\tLinux\n") is MacExecuteTerminalArgs


def test_resolver_result_is_usable_as_args_model_directly() -> None:
    """平台入口返回的类必须可直接作为 args_model 使用（含 model_dump 归一化）。"""

    resolved = resolve_execute_terminal_args_model("Windows")
    inst = resolved.model_validate({"command": "dir"})
    assert inst.model_dump() == {
        "shell": "auto",
        "command": "dir",
        "timeout": None,
        "workdir": None,
    }


@pytest.mark.parametrize(
    ("model", "forbidden_value"),
    [
        (WindowsExecuteTerminalArgs, "sh"),
        (WindowsExecuteTerminalArgs, "bash"),
        (WindowsExecuteTerminalArgs, "fish"),
        (MacExecuteTerminalArgs, "powershell"),
    ],
)
def test_platform_models_reject_every_foreign_shell_literal(
    model: type[BaseModel],
    forbidden_value: str,
) -> None:
    """逐项确认每个平台类都拒收对端 Literal（不能只有一个词被拦住）。"""

    result = validate_tool_arguments(
        {"command": "echo hi", "shell": forbidden_value},
        {},
        model,
    )
    assert result.ok is False, f"{model.__name__} 错误放行 shell={forbidden_value!r}"


def test_resolver_and_shell_detection_agree_on_casing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台选型与 shell 探测对平台名大小写的敏感度必须一致。

    观察到的实现差异：``resolve_execute_terminal_args_model`` 大小写不敏感
    （'windows' 也返回 Windows 类），而 ``ExecuteTerminalTool._detect_available_shells``
    用 ``platform_name == "Windows"`` 精确比较。若宿主上报小写平台名，二者会分叉：
    args_model 是 Windows 契约（Literal=auto/cmd/powershell/pwsh），枚举却走 POSIX 分支
    （auto/bash），导致「枚举 ⊆ Literal」不成立，模型拿到本平台不存在的 bash。
    本用例固定探测环境后断言二者判定一致；标准 ``platform.system()`` 上不会触发，
    属潜在健壮性缺口。
    """

    # 固定 Windows 探测环境：COMSPEC 存在、cmd/powershell 命中、/bin/sh 存在。
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    monkeypatch.setattr(
        f"{_TOOL_MODULE}.shutil.which",
        lambda name: {"cmd.exe": "C:/cmd.exe", "powershell.exe": "C:/ps.exe"}.get(name),
    )
    monkeypatch.setattr(f"{_TOOL_MODULE}.Path", _FakePath(bin_sh_exists=True))
    monkeypatch.setattr(f"{_TOOL_MODULE}.os.access", lambda *_a, **_k: True)

    for name in ("Windows", "windows", "WINDOWS"):
        model_is_windows = resolve_execute_terminal_args_model(name) is WindowsExecuteTerminalArgs
        detected = ExecuteTerminalTool._detect_available_shells(name)
        detects_windows = "cmd" in detected or "powershell" in detected
        assert model_is_windows == detects_windows, (
            f"平台名 {name!r} 下选型与探测分叉：model_is_windows={model_is_windows}, "
            f"detected={detected}"
        )


@pytest.mark.parametrize("model", [WindowsExecuteTerminalArgs, MacExecuteTerminalArgs])
def test_accepted_shells_match_declared_literals(model: type[BaseModel]) -> None:
    """类声明为 Literal 的每个取值都必须真的能通过校验（声明与实现一致）。"""

    literals = (
        get_args(WindowsExecuteTerminalShell)
        if model is WindowsExecuteTerminalArgs
        else get_args(MacExecuteTerminalShell)
    )
    for value in literals:
        result = validate_tool_arguments({"command": "echo hi", "shell": value}, {}, model)
        detail = f"{model.__name__} 拒绝了自身声明的 shell={value!r}: {result.error}"
        assert result.ok is True, detail


def test_extra_forbid_covers_all_known_fields_not_only_shell() -> None:
    """extra='forbid' 必须对四个字段都生效，而不是只拦 shell 的拼写错误。"""

    for typo in ("commandd", "timeoutt", "workdirr"):
        result = validate_tool_arguments(
            {"command": "x", typo: "y"},
            {},
            WindowsExecuteTerminalArgs,
        )
        assert result.ok is False, f"未知字段 {typo!r} 未被拒收"
        assert typo in result.error


def test_shell_description_mentions_host_availability_sentence_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Available on this host:' 只应出现一次，重复说明属冗余契约。"""

    for model in (WindowsExecuteTerminalArgs, MacExecuteTerminalArgs):
        description = str(model.model_json_schema()["properties"]["shell"]["description"])
        assert description.count("Available on this host:") == 0  # 静态类不含本机信息

    _patch_windows_environment(monkeypatch)
    tool_schema = _model_shell_schema(ExecuteTerminalTool())
    assert str(tool_schema["description"]).count("Available on this host:") == 1


# ----------------------------------------------------------------------
# 8. schema 装配的降级路径（契约被改坏时不得静默吞掉）
# ----------------------------------------------------------------------


class _ModelWithoutShell(BaseModel):
    """替身参数模型：故意不声明 shell 字段，触发误配告警分支。"""

    command: str = "echo hi"


def test_build_parameters_schema_warns_and_returns_raw_when_shell_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """参数模型缺 shell 属性时必须返回原始 schema 并记 WARNING，不能静默降级。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    tool.args_model = _ModelWithoutShell  # type: ignore[assignment]

    with caplog.at_level("WARNING"):
        schema = tool._build_parameters_schema()

    assert "shell" not in schema["properties"]
    assert "execute_terminal_shell_schema_missing" in caplog.text


def test_append_available_shells_handles_empty_description_without_leading_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """静态描述为空时只输出本机可用值句子，且不留前导空格。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    appended = tool._append_available_shells("")
    assert appended == "Available on this host: 'auto', 'cmd', 'powershell', 'pwsh'."
    assert appended == appended.lstrip()


def test_append_available_shells_joins_with_single_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非空静态描述与本机可用值之间必须恰好一个空格。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    appended = tool._append_available_shells("Pick a shell.")
    assert appended == "Pick a shell. Available on this host: 'auto', 'cmd', 'powershell', 'pwsh'."


# ----------------------------------------------------------------------
# 9. 平台契约与执行入口的一致性（不真正执行命令）
# ----------------------------------------------------------------------


def test_execute_rejects_shell_outside_detected_set_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute 要按本机实测结果复核 shell；未注入 execution_context 时先报上下文缺失。"""

    _patch_windows_environment(monkeypatch, cmd=True, powershell=False, pwsh=False)
    tool = ExecuteTerminalTool()

    missing_context = tool.execute(command="echo hi")
    assert missing_context.status == "error"
    assert "execution context is missing" in missing_context.error


def test_execute_reports_unavailable_shell_before_any_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """本机不可用的 shell 必须在校验/执行前被拒，并给出可选值列表。"""

    from app.core.tools.schemas import ToolExecutionContext

    _patch_windows_environment(monkeypatch, cmd=True, powershell=False, pwsh=False)
    tool = ExecuteTerminalTool()
    context = ToolExecutionContext(task_id=1, workspace_id=1, workspace_root=tmp_path)

    obs = tool.execute(  # noqa: S604 - 显式 shell 取值，harness 不启动真实 shell
        command="echo hi", shell="powershell", execution_context=context
    )
    assert obs.status == "error"
    assert "not available on this host" in obs.error
    assert "auto, cmd" in obs.reason


def test_resolve_workdir_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """workdir 解析：缺省=根、越界被拒、不存在被拒、根内相对路径可用。"""

    _patch_windows_environment(monkeypatch)
    tool = ExecuteTerminalTool()
    root = tmp_path
    (root / "sub").mkdir()

    assert tool._resolve_workdir(None, root) == (root.resolve(), "")
    assert tool._resolve_workdir("", root) == (root.resolve(), "")
    assert tool._resolve_workdir("sub", root) == ((root / "sub").resolve(), "")

    escaped, err = tool._resolve_workdir("..", root)
    assert escaped == Path()
    assert "escapes workspace root" in err

    missing, missing_err = tool._resolve_workdir("nope", root)
    assert "not found" in missing_err
    assert missing == Path()
