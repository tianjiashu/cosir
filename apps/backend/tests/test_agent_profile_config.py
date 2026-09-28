"""系统级和 workspace 子 Agent JSON 配置加载契约测试。"""

import json
import logging
from pathlib import Path

import pytest

from app.config.constant import Constant
from app.core.agents import agent_profile_config
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_config import initialize_system_agent_defaults
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.core.context import system_prompt_builder
from app.utils.token_estimator import TokenEstimator


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


def test_packaged_defaults_dir_is_optional_and_loads_nothing() -> None:
    """没有随包分发的默认 JSON 是合法状态：内置目录缺失时加载得到空结果。

    专用 CHILD（code-developer 等）已从 ``defaults/*.json`` 迁出仓库，唯一代码内置 CHILD
    由 ``define_agents.general_child_agent`` 直接注册；目录缺失不得让启动失败。
    """

    registry = AgentProfileRegistry()
    registry.load_agent_profiles(
        AgentProfileRegistry.SYSTEM_WORKSPACE,
        agent_profile_config._DEFAULTS_DIR,
    )

    assert registry.list(AgentProfileRegistry.SYSTEM_WORKSPACE) == []


def test_configured_prompt_enters_agent_layer_and_truncation_logs_agent_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """配置型提示词进入系统提示词层；超预算时只记录 profile ID。"""

    _write_config(tmp_path, "local-reader")
    profile = AgentProfile.vaild_agent_profile(tmp_path / "local-reader.json")
    monkeypatch.setattr(Constant.SystemPrompt, "AGENT_PERSONA_MAX_TOKENS", 3)

    layer = system_prompt_builder.SystemPromptBuilder._build_agent_layer(profile)

    body = layer[len("<agent_layer>\n") : -len("\n</agent_layer>")]
    assert 0 < TokenEstimator.estimate(body) <= 3
    assert body != profile.system_prompt
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
def test_loader_skips_invalid_config_with_error_log_and_hides_prompt(
    tmp_path: Path,
    name: str,
    data: dict | str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """损坏 schema 或工具权限配置不静默降级：返回 None 并写 error 日志，不泄漏正文。

    失败语义由 ``AgentProfile.vaild_agent_profile`` 持有：单个文件无效只影响该文件，
    异常被捕获、以 ``agent_profile_config_invalid`` 事件留痕后返回 ``None``（不再向外抛
    ``AgentProfileConfigError``）。
    """

    path = _write_config(tmp_path, name, data)

    with caplog.at_level(logging.ERROR):
        assert AgentProfile.vaild_agent_profile(path) is None

    records = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert records, "坏配置必须写 error 日志留痕"
    assert records[0].message == "agent_profile_config_invalid"
    assert records[0].data["file"] == str(path)
    assert records[0].data["error_type"]


def test_invalid_config_log_does_not_expose_prompt(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """坏配置的日志只带文件路径与异常摘要，不包含提示词正文。"""

    prompt = _document("leaky")["system_prompt"]
    path = _write_config(tmp_path, "leaky", {**_document("leaky"), "unknown_field": prompt})

    with caplog.at_level(logging.ERROR):
        assert AgentProfile.vaild_agent_profile(path) is None

    assert prompt not in caplog.text
    assert caplog.records[0].data["file"] == str(path)


def test_loader_does_not_check_filename_id_and_keeps_skipping_duplicates(
    tmp_path: Path,
) -> None:
    """文件名与 agent_id 不一致不是错误；同作用域重复 ID 跳过而不中断整批加载。

    文件名一致性属 ``AgentProfileRegistry`` 的已知缺口（见类 docstring）；重复 ID 由
    ``register`` 保留先注册者并告警，同样不抛错。
    """

    _write_config(tmp_path, "filename", _document("different-id"))
    mismatched = AgentProfileRegistry()
    mismatched.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, tmp_path)

    assert mismatched.list_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {"different-id"}

    (tmp_path / "filename.json").unlink()
    _write_config(tmp_path, "same-agent")
    _write_config(tmp_path, "same-agent-copy", _document("same-agent"))
    duplicated = AgentProfileRegistry()
    duplicated.load_agent_profiles(AgentProfileRegistry.SYSTEM_WORKSPACE, tmp_path)

    # 同作用域重复 ID（``same-agent-copy.json``）被跳过，不进入索引。
    assert duplicated.list_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {"same-agent"}


def test_workspace_catalog_isolated_and_rejects_baseline_collision(tmp_path: Path) -> None:
    """合并目录包含当前 workspace profile，且基线 Agent 不被 workspace 覆盖。"""

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    _write_config(directory, "workspace-reader")
    catalog = _base_registry()
    catalog.load_agent_profiles(workspace, directory)

    assert catalog.resolve(workspace, "general-assistant") is not None
    assert catalog.resolve(workspace, "workspace-reader") is not None
    assert catalog.resolve(workspace.parent / "other", "workspace-reader") is None

    # agent_id 与 system 基线冲突：保留先注册的 system profile，workspace 文件被跳过，
    # 整批加载不因冲突中断（冲突由 ``_decide_registration`` 裁决，不抛配置异常）。
    _write_config(directory, "general-assistant", _document("general-assistant"))
    _base_registry().load_agent_profiles(workspace, directory)

    collision = _base_registry()
    collision.load_agent_profiles(workspace, directory)
    resolved = collision.resolve(workspace, "general-assistant")
    assert resolved is not None
    assert resolved.description == general_child_agent().description
    # 同一目录内的非冲突文件仍正常装载。
    assert collision.resolve(workspace, "workspace-reader") is not None


def test_system_defaults_initialize_once_without_overwriting_user_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认导入只补缺失文件、不覆盖用户文件，且标记后不恢复用户删除项。

    当前仓库没有随包分发的默认 JSON，因此该入口在此只验证标记与「不覆盖」语义：用户自定义
    文件保持原样，标记落盘后重复调用是幂等的。
    """

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_config, "system_agent_config_dir", lambda: target)
    target.mkdir()
    custom = _write_config(target, "code-explorer", _document("code-explorer") | {"role": "custom"})

    assert initialize_system_agent_defaults() == target
    assert json.loads(custom.read_text(encoding="utf-8"))["role"] == "custom"
    assert (target / agent_profile_config._DEFAULTS_MARKER).is_file()

    initialize_system_agent_defaults()

    assert json.loads(custom.read_text(encoding="utf-8"))["role"] == "custom"
    assert (target / agent_profile_config._DEFAULTS_MARKER).is_file()
