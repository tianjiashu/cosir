"""Agent Team 节点 Run 执行 profile 的模型运行设置契约测试。

背景（已在真实运行中复现的缺陷）：内置子 Agent（如 ``general-assistant``）的注册表共享单例
只有用户偏好项、没有模型连接与能力字段。若节点启动时直接用注册表 profile 的
``model_config_id`` / ``model_settings`` / ``structured_output`` 构造执行 profile，
``resolve_chat_model`` 会在 ``ModelSettings.require_runtime_config`` 处以
``model_resolve_failed`` 立刻失败（节点 Run ``end_reason=model_resolve_failed``）。

本文件只消费生产代码（``app/**`` 只读），锁定三条行为契约：

1. 节点执行 profile（``AgentTeamCoordinator._build_node_profile`` 合成基础 profile，
   ``run_driver._resolve_profile`` 按 Run 派生）必须带**已物化**的
   模型运行设置（连接与能力字段来自 ``run.model_config_id`` 指向的模型配置），并保留节点快照的
   用户偏好、采用节点快照的 ``structured_output``、绑定 ``run``；
2. 节点快照不是连接字段的来源：快照里出现的任何连接字段都不得影响最终执行 profile；
3. 准备阶段的节点快照只冻结可持久化的用户偏好，不得把 ``api_key`` 等运行期字段写进
   ``agent_team_runs.state_json``。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.coordinator import AgentTeamCoordinator
from app.agent_team.node_goal import AgentTeamNodeGoal
from app.agent_team.state.agent_team_run_state import AgentTeamRunRuntime
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.core.agents.structured_output_spec import StructuredOutputSpec
from app.models.conversation_run_options import ConversationRunOptions
from app.models.conversation_run_record import ConversationRunRecord
from app.service.agent_team import agent_team_preparation_service as prep_module
from app.service.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationService,
)
from app.service.conversation_run import run_driver
from app.service.depends import get_model_config_service

# --------------------------------------------------------------------------- #
# 构造助手
# --------------------------------------------------------------------------- #


_NODE_SCHEMA: dict[str, object] = {
    "type": "object",
    "description": "node outcome contract",
    "properties": {
        "status": {"type": "string"},
        "output": {"type": "string"},
    },
    "required": ["status", "output"],
    "additionalProperties": False,
}


def _registry_child_profile() -> AgentProfile:
    """构造内置子 Agent 形态的注册表共享单例：只有用户偏好，没有连接与能力字段。

    潜在线索（与本文件断言直接相关）：该形态下 ``model_settings.model_name`` /
    ``base_url`` / ``context_window_k`` 全为 ``None``，``model_config_id`` 为 ``None``，
    因此任何直接拿它执行模型的路径都会在 ``require_runtime_config`` 失败。
    """

    return AgentProfile(
        agent_id="general-assistant",
        role="general-assistant",
        description="通用子 Agent",
        system_prompt="registry child system prompt",
        allowed_tools=["read_file", "write_file"],
        agent_type=AgentProfileType.CHILD,
        model_config_id=None,
        model_settings=ModelSettings(stream=True, reasoning_effort="high"),
        max_steps=300,
    )


def _node_structured_output() -> StructuredOutputSpec:
    """构造节点快照里冻结的结构化输出契约。"""

    return StructuredOutputSpec(name="agent_team_node_result", json_schema=_NODE_SCHEMA)


def _node_runtime(
    model_config_id: int,
    *,
    role: str = "develop-role",
    model_settings: dict[str, object] | None = None,
) -> AgentTeamRunRuntime:
    """构造确认时冻结的节点运行快照（``agent_team_runs.state_json`` 内的形态）。"""

    return AgentTeamRunRuntime(
        agent_id="general-assistant",
        role=role,
        node_goal="实现登录",
        node_goal_summary="实现登录",
        system_prompt="node system prompt from snapshot",
        allowed_tools=["read_file", "web_search"],
        structured_output=_node_structured_output().to_document(),
        model_config_id=model_config_id,
        # 与 ``AgentTeamPreparationService`` 产出的快照同形：只含用户偏好项。
        model_settings={"stream": True, "reasoning_effort": "low"}
        if model_settings is None
        else model_settings,
    )


def _node_run(
    model_config_id: int,
    *,
    reasoning_effort: str | None = None,
) -> ConversationRunRecord:
    """构造节点 Run 记录；``model_config_id`` 是连接字段的唯一来源。"""

    now = datetime.now(UTC)
    return ConversationRunRecord(
        id=101,
        task_id=7,
        status="running",
        created_at=now,
        updated_at=now,
        agent_id="general-assistant",
        model_config_id=model_config_id,
        options=ConversationRunOptions(
            reasoning_effort=reasoning_effort,
        ),
    )


def _node_run_profile(
    registry_profile: AgentProfile,
    node_runtime: AgentTeamRunRuntime,
    node_run: ConversationRunRecord,
) -> AgentProfile:
    """生产两段串联：协调器合成节点基础 profile，运行时按节点 Run 派生并物化模型设置。"""

    base = AgentTeamCoordinator._build_node_profile(registry_profile, node_runtime)
    return run_driver._resolve_profile(node_run, SimpleNamespace(root_path=""), base)


def _create_model_config(*, model_name: str, base_url: str) -> int:
    """写入一条真实 ``model_configs`` 记录并返回其主键。"""

    record = get_model_config_service().create_config(
        config_name=f"team-node-{model_name}",
        base_url=base_url,
        api_key=f"{model_name}-secret",
        model_name=model_name,
        context_window_k=96,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )
    assert record.id is not None
    return record.id


def _stub_preparation_registry(monkeypatch: pytest.MonkeyPatch, child: AgentProfile) -> None:
    """把准备服务对 Agent 注册表的查找替换为最小桩，保留装配逻辑本体。"""

    registry = SimpleNamespace(resolve=lambda scope, agent_id: child)
    monkeypatch.setattr(prep_module, "get_agent_registry", lambda: registry)


def _team_configuration() -> dict[str, object]:
    """构造一份通过领域校验的两个节点 Team 配置。"""

    return {
        "team_id": "code-quality",
        "name": "代码质量 Team",
        "description": "开发、审查和测试",
        "scope": "workspace",
        "start_node_id": "develop",
        "nodes": [
            {
                "node_id": "develop",
                "name": "开发",
                "agent_id": "general-assistant",
                "statuses": [
                    {"name": "done", "color": "#16A34A"},
                    {"name": "blocked", "color": "#DC2626"},
                ],
            },
            {
                "node_id": "review",
                "name": "审查",
                "agent_id": "general-assistant",
                "statuses": [
                    {"name": "passed", "color": "#16A34A"},
                    {"name": "needs_changes", "color": "#F59E0B"},
                ],
            },
        ],
        "transitions": [
            {"from_node_id": "develop", "status": "done", "target_node_id": "review"},
            {"from_node_id": "develop", "status": "blocked", "target_node_id": "END"},
            {"from_node_id": "review", "status": "passed", "target_node_id": "END"},
            {"from_node_id": "review", "status": "needs_changes", "target_node_id": "develop"},
        ],
    }


def _materialized_parent_profile() -> AgentProfile:
    """构造主 Agent 本次 Run 的 per-run profile（连接字段已物化）。"""

    return AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        system_prompt="main system prompt",
        allowed_tools=["agent_team", "read_file"],
        agent_type=AgentProfileType.MAIN,
        model_config_id=1,
        model_settings=ModelSettings(
            base_url="http://127.0.0.1/v1",
            api_key="main-secret",
            model_name="main-model",
            context_window_k=32,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_image=False,
        ),
    )


# --------------------------------------------------------------------------- #
# 契约 1：节点 Run 的执行 profile 必须带已物化的模型运行设置
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("backend_env")
def test_node_run_profile_materializes_model_runtime_settings_from_run() -> None:
    """(回归) 节点 Run 的 per-run profile 必须经 ``run.model_config_id`` 物化出完整运行设置。

    潜在缺陷：用注册表共享子 profile 的 ``model_settings`` / ``model_config_id`` 直接构造执行
    profile（内置子 Agent 只有偏好项），``resolve_chat_model`` 会在
    ``ModelSettings.require_runtime_config`` 处抛 ``ModelSettingsError``，节点 Run 立即以
    ``model_resolve_failed`` 失败、整条 TeamRun 随之失败。
    """

    config_id = _create_model_config(
        model_name="team-node-model", base_url="https://team.example.test/v1"
    )
    registry_profile = _registry_child_profile()
    node_runtime = _node_runtime(config_id)
    # run.options 刻意不设置 reasoning_effort：快照偏好必须是唯一的来源。
    node_run = _node_run(config_id, reasoning_effort=None)

    profile = _node_run_profile(registry_profile, node_runtime, node_run)

    settings = profile.model_settings
    # 未物化时这里会抛 ModelSettingsError（模型运行配置未物化，缺少字段: model_name, ...）。
    settings.require_runtime_config()
    assert settings.model_name == "team-node-model"
    assert settings.base_url == "https://team.example.test/v1"
    assert settings.api_key == "team-node-model-secret"
    assert settings.context_window_k == 96
    assert settings.supports_thinking is True
    assert settings.supports_reasoning_effort is True
    assert settings.supports_image is False
    # 节点快照的用户偏好被保留（未被本次物化的空偏好顶掉）。
    assert settings.reasoning_effort == "low"
    assert settings.stream is True

    # 结构化输出取自节点快照，而非注册表 profile 的 None。
    assert profile.structured_output == _node_structured_output()
    assert registry_profile.structured_output is None
    # 身份字段来自注册表 profile；快照角色优先。
    assert profile.agent_id == registry_profile.agent_id
    assert profile.agent_type is AgentProfileType.CHILD
    assert profile.max_steps == registry_profile.max_steps
    assert profile.role == "develop-role"
    assert registry_profile.role == "general-assistant"
    # 提示词与工具名来自节点快照，且工具名是可独立修改的副本。
    assert profile.system_prompt == "node system prompt from snapshot"
    assert profile.allowed_tools == ["read_file", "web_search"]
    assert profile.allowed_tools is not node_runtime.allowed_tools
    # Run 绑定在派生副本上，本次 Run 的模型配置覆盖 profile 的（空）选择。
    assert profile.run is node_run
    assert profile.model_config_id == config_id
    assert registry_profile.model_config_id is None
    # 共享注册表单例不得被本次派生污染。
    assert registry_profile.model_settings.model_name is None
    assert registry_profile.model_settings.reasoning_effort == "high"
    assert registry_profile.run is None


@pytest.mark.usefixtures("backend_env")
def test_node_run_profile_takes_connection_fields_from_run_not_from_snapshot() -> None:
    """(对抗/边界) 连接字段只认 ``run.model_config_id``；快照里的连接字段不得生效。

    潜在缺陷：把节点快照中的 ``model_settings`` 当作模型连接来源（旧实现甚至直接取注册表
    profile），使快照里被篡改/漂移的连接地址或密钥成为真实执行参数；或 Run 换了模型配置后
    仍沿用确认时的旧连接信息。
    """

    first_config_id = _create_model_config(
        model_name="snapshot-model", base_url="https://snapshot.example.test"
    )
    second_config_id = _create_model_config(
        model_name="run-model", base_url="https://run.example.test"
    )
    assert first_config_id != second_config_id
    registry_profile = _registry_child_profile()
    # 快照刻意携带一份「伪连接字段」，它在持久化契约里本不应存在。
    node_runtime = _node_runtime(
        first_config_id,
        model_settings={
            "reasoning_effort": "max",
            "base_url": "https://snapshot.example.test",
            "api_key": "snapshot-secret",
            "model_name": "snapshot-model",
            "context_window_k": 8,
            "supports_thinking": False,
        },
    )
    node_run = _node_run(second_config_id)

    profile = _node_run_profile(registry_profile, node_runtime, node_run)

    settings = profile.model_settings.require_runtime_config()
    assert settings.model_name == "run-model"
    assert settings.base_url == "https://run.example.test"
    assert settings.api_key == "run-model-secret"
    assert settings.context_window_k == 96
    assert settings.supports_thinking is True
    # 快照的偏好项仍然是偏好项的唯一来源。
    assert settings.reasoning_effort == "max"
    assert profile.model_config_id == second_config_id


@pytest.mark.usefixtures("backend_env")
def test_unmaterialized_registry_settings_would_fail_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(缺陷证明) 还原修复前「直接用注册表未物化设置」时，上面的断言必然失败。

    目的：证明 ``test_node_run_profile_materializes_model_runtime_settings_from_run`` 的
    ``require_runtime_config()`` 断言真的能捕获本缺陷——一旦物化被去掉（回到旧行为），
    合成的 profile 立刻以 ``model_name`` / ``base_url`` 等字段缺失失败。
    """

    config_id = _create_model_config(
        model_name="team-node-model", base_url="https://team.example.test/v1"
    )
    registry_profile = _registry_child_profile()
    node_runtime = _node_runtime(config_id)
    node_run = _node_run(config_id)
    # 旧行为：执行 profile 直接沿用注册表 profile 的（未物化）模型设置。
    monkeypatch.setattr(
        run_driver.ModelSettings,
        "from_model_config_record",
        staticmethod(lambda _record: registry_profile.model_settings),
    )

    profile = _node_run_profile(registry_profile, node_runtime, node_run)

    with pytest.raises(ModelSettingsError) as captured:
        profile.model_settings.require_runtime_config()
    message = str(captured.value)
    # ``context_window_k`` 改为在缺失字段全部齐备后单独做正整数校验，不进入缺失清单。
    for missing in (
        "model_name",
        "base_url",
        "supports_thinking",
        "supports_reasoning_effort",
        "supports_image",
    ):
        assert missing in message


def test_legacy_registry_profile_construction_reproduces_runtime_failure() -> None:
    """(缺陷证据) 复刻修复前 ``_start_node`` 的 profile 构造方式，稳定复现线上失败签名。

    修复前的节点执行 profile 直接取自注册表共享子 profile 的
    ``model_config_id`` / ``model_settings`` / ``structured_output``，并用未物化的
    ``model_settings`` 执行。这里用等价构造证明：该路径必然在
    ``require_runtime_config`` 抛 ``ModelSettingsError``（与线上日志
    ``core/llm_provider/model_factory.py`` → ``model_settings.require_runtime_config`` 一致），
    且节点快照的结构化输出契约被整体丢弃——即上面两条用例的断言确有区分度。
    """

    registry_profile = _registry_child_profile()
    node_run = _node_run(1)
    legacy_profile = AgentProfile(
        agent_id="general-assistant",
        role=registry_profile.role,
        system_prompt="node system prompt from snapshot",
        allowed_tools=["read_file", "web_search"],
        agent_type=registry_profile.agent_type,
        model_config_id=registry_profile.model_config_id,
        model_settings=registry_profile.model_settings,
        structured_output=registry_profile.structured_output,
        max_steps=registry_profile.max_steps,
        run=node_run,
    )

    assert legacy_profile.model_config_id is None
    # 快照里明确存在的结构化输出契约被旧构造丢弃。
    assert legacy_profile.structured_output is None
    assert _node_structured_output().json_schema == _NODE_SCHEMA
    with pytest.raises(ModelSettingsError) as captured:
        legacy_profile.model_settings.require_runtime_config()
    assert str(captured.value) == (
        "模型运行配置未物化，缺少字段: model_name, base_url, "
        "supports_thinking, supports_reasoning_effort, supports_image"
    )


@pytest.mark.usefixtures("backend_env")
def test_node_run_without_model_config_keeps_unmaterialized_settings() -> None:
    """(边界) Run 未绑定模型配置时不得凭空造出连接字段，仍沿用注册表偏好（后续由模型层报错）。

    潜在缺陷：为「未选模型」的 Run 伪造默认 base_url / model_name，让执行悄悄跑在一个
    用户从未选择的模型上；或反向在此路径抛未捕获异常。
    """

    registry_profile = _registry_child_profile()
    node_runtime = _node_runtime(1, model_settings={"reasoning_effort": "low"})
    node_run = _node_run(1)
    node_run.model_config_id = None

    profile = _node_run_profile(registry_profile, node_runtime, node_run)

    assert profile.model_settings.model_name is None
    assert profile.model_settings.base_url is None
    assert profile.model_settings.reasoning_effort == "low"
    assert profile.model_config_id is None
    with pytest.raises(ModelSettingsError):
        profile.model_settings.require_runtime_config()


# --------------------------------------------------------------------------- #
# 契约 3：准备阶段的节点快照只冻结用户偏好，不落运行期连接字段
# --------------------------------------------------------------------------- #


_RUNTIME_ONLY_FIELDS = (
    "api_key",
    "base_url",
    "model_name",
    "context_window_k",
    "supports_thinking",
    "supports_reasoning_effort",
    "supports_image",
)


def test_preparation_snapshot_freezes_preferences_without_connection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(防密钥回归) 节点快照的 ``model_settings`` 只含用户偏好，不含连接/能力/密钥字段。

    潜在缺陷：用 ``dataclasses.asdict`` 之类的全字段序列化把 ``api_key``、``base_url``、
    ``model_name`` 一起写进 ``agent_team_runs.state_json``，造成密钥随快照落库（并可能随
    API 投影泄漏）；同时让执行期误以为快照已带完整模型配置。
    """

    child = _registry_child_profile()
    _stub_preparation_registry(monkeypatch, child)
    parent = _materialized_parent_profile()

    result = AgentTeamPreparationService().prepare(
        AgentTeamConfiguration.model_validate(_team_configuration()),
        goal="交付报告",
        node_goals={
            "develop": AgentTeamNodeGoal(goal="实现", summary="实现"),
            "review": AgentTeamNodeGoal(goal="审查", summary="审查"),
        },
        workspace_root="ws",
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        parent_agent_profile=parent,
    )

    assert set(result.node_runtime_snapshots) == {"develop", "review"}
    for node_id, snapshot in result.node_runtime_snapshots.items():
        frozen = snapshot["model_settings"]
        for leaked in _RUNTIME_ONLY_FIELDS:
            assert (
                leaked not in frozen
            ), f"{node_id} 快照的 model_settings 泄漏了运行期字段 {leaked}"
        # 偏好项必须保留（执行期靠它 + run.model_config_id 还原完整设置）。
        assert frozen["reasoning_effort"] == "high"
        assert frozen["stream"] is True
        # 兜底：整个快照序列化后不得出现父 Run 的密钥。
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "main-secret" not in serialized
        assert "http://127.0.0.1/v1" not in serialized
        # 连接来源以 ``model_config_id`` 表达，由主 Agent 本次 Run 的配置补全。
        assert snapshot["model_config_id"] == parent.model_config_id
        assert snapshot["agent_id"] == "general-assistant"
