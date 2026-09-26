"""系统级和 workspace 子 Agent JSON 配置加载契约测试。"""

import json
from pathlib import Path

import pytest

from app.config.constant import Constant
from app.core.agents import agent_profile_config
from app.core.agents.agent_profile import AgentProfile, AgentProfileConfigError
from app.core.agents.agent_profile_config import initialize_system_agent_defaults
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.core.context import system_prompt_builder


def _document(agent_id: str) -> dict:
    """返回最小有效 CHILD 配置文档。"""

    return {
        "agent_id": agent_id,
        "role": "reader",
        "description": "Read a bounded part of the workspace.",
        "system_prompt": "Read only the files needed and report evidence.",
        "allowed_tools": ["read_file"],
    }


def _write_config(directory: Path, agent_id: str, data: dict | str | None = None) -> Path:
    """在目录中写入测试配置。"""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_id}.json"
    content = _document(agent_id) if data is None else data
    path.write_text(
        json.dumps(content) if isinstance(content, dict) else content,
        encoding="utf-8",
    )
    return path


def _base_registry() -> AgentProfileRegistry:
    """构造包含代码内通用 Agent 和主 Agent 的进程目录。"""

    registry = AgentProfileRegistry()
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, general_child_agent())
    registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, main_agent())
    return registry


def test_loader_builds_child_profile_with_inline_prompt(tmp_path: Path) -> None:
    """JSON 提示词进入内存 profile，不生成提示词文件路径。"""

    path = _write_config(tmp_path, "local-reader")

    profile = AgentProfile.vaild_agent_profile(path)

    assert profile.agent_id == "local-reader"
    assert profile.system_prompt == _document("local-reader")["system_prompt"]
    assert not hasattr(profile, "prompt_file_path")
    assert path.exists()


def test_packaged_default_profiles_are_valid_and_exclude_generic_child() -> None:
    """随应用分发的默认 JSON 合法，且通用代码内 CHILD 不重复配置。"""

    registry = AgentProfileRegistry()
    registry.load_agent_profiles(
        AgentProfileRegistry.SYSTEM_WORKSPACE,
        agent_profile_config._DEFAULTS_DIR,
    )
    profiles = registry.list(AgentProfileRegistry.SYSTEM_WORKSPACE)

    assert {profile.agent_id for profile in profiles} == {
        "code-developer",
        "code-explorer",
        "delegate_reviewer",
        "unit-test-engineer",
    }
    assert all(profile.system_prompt for profile in profiles)
    assert "general-assistant" not in {profile.agent_id for profile in profiles}


def test_configured_prompt_enters_agent_layer_and_truncation_logs_agent_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """配置型提示词进入系统提示词层；超预算时只记录 profile ID。"""

    _write_config(tmp_path, "local-reader")
    profile = AgentProfile.vaild_agent_profile(tmp_path / "local-reader.json")
    monkeypatch.setattr(
        Constant.SystemPrompt,
        "AGENT_PERSONA_MAX_BYTES",
        12,
    )
    monkeypatch.setattr(
        Constant.SystemPrompt,
        "AGENT_PERSONA_MAX_TOKENS",
        1000,
    )

    layer = system_prompt_builder.SystemPromptBuilder._build_agent_layer(profile)

    assert "Read only the files" not in layer
    assert "agent_system_prompt_truncated" in caplog.text
    assert caplog.records[0].data == {"agent_id": "local-reader"}


@pytest.mark.parametrize(
    "name,data",
    [
        ("broken", "{"),
        ("missing-prompt", {**_document("missing-prompt"), "system_prompt": "  "}),
        ("unknown-tool", {**_document("unknown-tool"), "allowed_tools": ["not-a-tool"]}),
        ("unknown-setting", {**_document("unknown-setting"), "model_settings": {"api_key": "x"}}),
    ],
)
def test_loader_rejects_invalid_config_without_exposing_prompt(
    tmp_path: Path,
    name: str,
    data: dict | str,
) -> None:
    """损坏 schema 或工具权限配置不会静默降级。"""

    _write_config(tmp_path, name, data)

    with pytest.raises(AgentProfileConfigError):
        AgentProfile.vaild_agent_profile(tmp_path / f"{name}.json")


def test_loader_rejects_filename_id_mismatch_and_duplicate(tmp_path: Path) -> None:
    """同一目录的重复 ID 和文件名不匹配都作为配置错误。"""

    _write_config(tmp_path, "filename", _document("different-id"))
    registry = AgentProfileRegistry()
    with pytest.raises(AgentProfileConfigError, match="文件名必须与 agent_id 一致"):
        registry.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, tmp_path)

    (tmp_path / "filename.json").unlink()
    _write_config(tmp_path, "same-agent")
    _write_config(tmp_path, "same-agent-copy", _document("same-agent"))
    with pytest.raises(AgentProfileConfigError, match="文件名必须与 agent_id 一致"):
        registry.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, tmp_path)


def test_workspace_catalog_isolated_and_rejects_baseline_collision(tmp_path: Path) -> None:
    """合并目录包含当前 workspace profile，禁止覆盖基线 Agent。"""

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    _write_config(directory, "workspace-reader")
    catalog = _base_registry()
    catalog.load_agent_profiles(workspace, directory)

    assert catalog.resolve(workspace, "general-assistant") is not None
    assert catalog.resolve(workspace, "workspace-reader") is not None
    assert catalog.resolve(workspace.parent / "other", "workspace-reader") is None

    _write_config(directory, "general-assistant", _document("general-assistant"))
    with pytest.raises(AgentProfileConfigError, match="profile 冲突"):
        _base_registry().load_agent_profiles(workspace, directory)


def test_system_defaults_initialize_once_without_overwriting_user_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 JSON 原子导入且不覆盖用户已有文件或恢复标记后的删除项。"""

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_config, "system_agent_config_dir", lambda: target)
    target.mkdir()
    custom = _write_config(target, "code-explorer", _document("code-explorer") | {"role": "custom"})

    assert initialize_system_agent_defaults() == target
    assert json.loads(custom.read_text(encoding="utf-8"))["role"] == "custom"
    (target / "delegate_reviewer.json").unlink()

    initialize_system_agent_defaults()

    assert not (target / "delegate_reviewer.json").exists()
    assert (target / agent_profile_config._DEFAULTS_MARKER).is_file()
