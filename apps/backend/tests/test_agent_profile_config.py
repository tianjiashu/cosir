"""系统级和 workspace 子 Agent JSON 配置加载契约测试。"""

import json
import logging
from pathlib import Path

import pytest

from app.config.constant import Constant
from app.core.agents import agent_profile_source
from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_config import ensure_system_agent_config_dir
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_profile_source import AgentProfileScopeSource
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


def _system_source(monkeypatch: pytest.MonkeyPatch, directory: Path) -> AgentProfileScopeSource:
    """构造 system 作用域指向指定目录的 profile 来源，隔离开发者本机 ``~/.cosir``。"""

    monkeypatch.setattr(agent_profile_source, "system_agent_config_dir", lambda: directory)
    return AgentProfileScopeSource()


def _base_registry() -> AgentProfileRegistry:
    """构造包含代码内通用 Agent 和主 Agent 的进程目录。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
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


def test_system_agent_config_dir_creates_empty_workspace_when_no_packaged_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有随包分发的默认 JSON 时，系统 Agent 配置目录仍会被创建且装载为空。

    专用 CHILD（code-developer 等）已从内置 defaults 迁出仓库，唯一代码内置 CHILD 由
    ``define_agents.general_child_agent`` 直接注册；目录存在但无 JSON 不得让装载失败。
    """

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_source, "system_agent_config_dir", lambda: target)

    ensure_system_agent_config_dir()
    assert target.is_dir()

    registry = AgentProfileRegistry(_system_source(monkeypatch, target))

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件名与 agent_id 不一致不是错误；同作用域重复 ID 跳过而不中断整批装载。

    文件名一致性属 ``AgentProfileRegistry`` 的已知缺口（见类 docstring）；重复 ID 由
    ``_decide_registration`` 保留先读到的文件并告警，同样不抛错。
    """

    _write_config(tmp_path, "filename", _document("different-id"))
    mismatched = AgentProfileRegistry(_system_source(monkeypatch, tmp_path))

    assert mismatched.list_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {"different-id"}

    (tmp_path / "filename.json").unlink()
    _write_config(tmp_path, "same-agent")
    _write_config(tmp_path, "same-agent-copy", _document("same-agent"))
    duplicated = AgentProfileRegistry(_system_source(monkeypatch, tmp_path))

    # 同作用域重复 ID（``same-agent-copy.json``）被跳过，不进入索引。
    assert duplicated.list_agent_ids(AgentProfileRegistry.SYSTEM_WORKSPACE) == {"same-agent"}


def test_workspace_catalog_isolated_and_rejects_baseline_collision(tmp_path: Path) -> None:
    """合并目录包含当前 workspace profile，且基线 Agent 不被 workspace 覆盖。"""

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    _write_config(directory, "workspace-reader")
    catalog = _base_registry()

    assert catalog.resolve(workspace, "general-assistant") is not None
    assert catalog.resolve(workspace, "workspace-reader") is not None
    assert catalog.resolve(workspace.parent / "other", "workspace-reader") is None

    # agent_id 与 system 基线冲突：保留先注册的 system profile，workspace 文件被跳过，
    # 装载不因冲突中断（冲突由 ``_decide_registration`` 裁决，不抛配置异常）。
    _write_config(directory, "general-assistant", _document("general-assistant"))

    collision = _base_registry()
    resolved = collision.resolve(workspace, "general-assistant")
    assert resolved is not None
    assert resolved.description == general_child_agent().description
    # 同一目录内的非冲突文件仍正常装载。
    assert collision.resolve(workspace, "workspace-reader") is not None


def test_system_agent_config_dir_ensured_without_touching_user_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动期确保系统 .cosir/agents 目录存在，且不改动用户已有的自定义 Agent 文件。

    当前仓库不随包分发默认 Agent JSON，因此该入口只验证目录创建与「不触碰已有文件」语义：
    用户自定义文件保持原样，重复调用是幂等的。
    """

    target = tmp_path / "system-agents"
    monkeypatch.setattr(agent_profile_source, "system_agent_config_dir", lambda: target)

    assert ensure_system_agent_config_dir() == target
    assert target.is_dir()

    custom = _write_config(target, "code-explorer", _document("code-explorer") | {"role": "custom"})
    assert json.loads(custom.read_text(encoding="utf-8"))["role"] == "custom"

    ensure_system_agent_config_dir()

    assert json.loads(custom.read_text(encoding="utf-8"))["role"] == "custom"
