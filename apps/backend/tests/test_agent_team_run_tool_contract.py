"""``agent_team`` 执行工具的契约、两阶段语义与边界对抗测试。

本文件只消费生产代码（``app/**`` 只读），不修改任何实现。测试从三类入口交叉验证定义：
直构 ``ToolDefinition`` / ``ToolRegistry`` 归一化后读回 / 工具注册表进程装配后读回；
并覆盖 ``AgentTeamRunTool.execute()`` 的「准备（``user_decision is None``）」与
「启动（APPROVE）」两条分支、``team_register.resolve`` 返回 ``None`` 的失败契约、
确认分支的失败映射，以及准备服务的边界输入。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config.configuration import get_tool_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.tool.handler.agent_team.args.agent_team_args import AgentTeamArgs
from app.core.tool.handler.agent_team.args.node_goal import AgentTeamNodeGoal
from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.core.tool.handler.agent_team_run import AgentTeamRunTool, build_agent_team_run_definition
from app.core.tool.schemas import ToolExecutionContext
from app.core.tool.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tool.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tool.tool_registry import ToolRegistry
from app.models.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.models.model_config_record import ModelConfigRecord
from app.core.tool.handler.agent_team import agent_team_preparation_service as prep_module
from app.service.configuration import agent_team_configuration_registry as team_registry_module
from app.core.tool.handler.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationService,
)
from app.service.configuration.agent_team_configuration_source import AgentTeamConfigurationScopeSource
from app.service.configuration.agent_team_configuration_registry import AgentTeamConfigurationRegistry

# --------------------------------------------------------------------------- #
# 夹具与构造助手
# --------------------------------------------------------------------------- #


def _node_inputs(**goals: str) -> list[dict[str, str]]:
    """模型输入使用显式节点列表；领域服务与批准输入仍使用节点映射。"""
    return [{"node_id": node_id, "goal": goal, "summary": goal} for node_id, goal in goals.items()]


def _node_goal_map(**goals: str) -> dict[str, AgentTeamNodeGoal]:
    """准备服务消费的节点目标映射（目标与展示摘要同文）。"""
    return {node_id: AgentTeamNodeGoal(goal=goal, summary=goal) for node_id, goal in goals.items()}


def _configuration(**overrides: object) -> dict[str, object]:
    """构造一份通过领域校验的 Team 配置文档（两个节点、均可到达 END）。"""

    document: dict[str, object] = {
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
                "statuses": [{"name": "done", "color": "#16A34A"}, {"name": "blocked", "color": "#DC2626"}],
            },
            {
                "node_id": "review",
                "name": "审查",
                "agent_id": "general-assistant",
                "statuses": [{"name": "passed", "color": "#16A34A"}, {"name": "needs_changes", "color": "#F59E0B"}],
            },
        ],
        "transitions": [
            {"from_node_id": "develop", "status": "done", "target_node_id": "review"},
            {"from_node_id": "develop", "status": "blocked", "target_node_id": "END"},
            {"from_node_id": "review", "status": "passed", "target_node_id": "END"},
            {"from_node_id": "review", "status": "needs_changes", "target_node_id": "develop"},
        ],
    }
    document.update(overrides)
    return document


def _definition():
    """仅用类元数据构造定义，绕开 ``AgentTeamRunTool.__init__`` 的进程单例依赖。"""

    return AgentTeamRunTool.to_definition(object.__new__(AgentTeamRunTool))


def _bare_tool() -> AgentTeamRunTool:
    """构造不触达存储 / 进程单例的裸实例（由用例逐个注入协作对象）。"""

    return object.__new__(AgentTeamRunTool)


def _context() -> ToolExecutionContext:
    """构造带父 Run 事实的执行上下文。

    生产上下文的 ``agent`` 由 runtime 注入本次 Run 的 profile；工具依赖它准备节点模型设置，
    因此这里默认注入一份已物化的父 profile。需要验证「未注入」口径的用例直接自行构造
    ``ToolExecutionContext``。
    """

    return ToolExecutionContext(
        step_id="step-1",
        task_id=1,
        workspace_id=1,
        workspace_root=Path("C:/ws-root"),
        run_id=2,
        agent=_parent_run_profile(),
        tool_call_id="call-team",
    )


def _child_profile() -> AgentProfile:
    """构造一个选定了模型配置的 CHILD profile，使准备阶段能通过模型配置解析。"""

    return AgentProfile(
        agent_id="general-assistant",
        role="general-assistant",
        description="通用子 Agent",
        system_prompt="child system prompt",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        model_config_id=1,
    )


# 测试用模型配置：真实链路由 ``model_config_id`` 从模型配置 service 读出，这里用等价记录，
# 避免用例依赖数据库。
_MODEL_CONFIG = ModelConfigRecord(
    id=1,
    config_name="local",
    base_url="http://127.0.0.1/v1",
    api_key="secret",
    model_name="local-model",
    context_window_k=32,
    supports_thinking=True,
    supports_reasoning_effort=True,
)


def _runtime(definition: AgentProfile) -> AgentRuntimeConfig:
    """按测试模型配置解析运行时配置。"""

    return AgentRuntimeConfig.resolve(definition, _MODEL_CONFIG)


def _parent_run_profile() -> AgentRuntimeConfig:
    """构造本次 Run 由运行时注入的父 Agent 运行时配置（主 Agent + 已物化模型设置）。

    潜在缺陷：若调用方改用注册表共享单例（内置 profile 的 ``model_settings`` 只有偏好项、
    ``model_config_id`` 为空），节点模型设置回落会走 ``get_config(None)`` 抛 ``KeyError``。
    """

    return _runtime(
        AgentProfile(
            agent_id="main_agent",
            role="main_agent",
            system_prompt="main system prompt",
            allowed_tools=["agent_team", "read_file"],
            agent_type=AgentProfileType.MAIN,
            model_config_id=1,
        )
    )


def _stub_preparation_lookups(monkeypatch: pytest.MonkeyPatch, child: AgentProfile) -> None:
    """把准备服务对 Agent 注册表的查找替换为最小桩，保留装配逻辑本体。

    节点工具来自 Profile 的 ``allowed_tools``（不经工具系统解析 schema），因此这里只需桩掉
    节点 Profile 的解析来源。
    """

    registry = SimpleNamespace(resolve=lambda scope, agent_id: child)
    monkeypatch.setattr(prep_module, "get_agent_registry", lambda: registry)
    monkeypatch.setattr(
        prep_module,
        "get_model_config_service",
        lambda: SimpleNamespace(get_config=lambda _config_id: _MODEL_CONFIG),
    )


@pytest.fixture
def assembled_tool_registry(isolated_storage, install_process_singletons, monkeypatch):
    """装配真实工具注册表：按 lifespan 的顺序先注入 Team 注册表，再构建单例。"""

    monkeypatch.setattr(
        team_registry_module,
        "_REGISTRY",
        AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource()),
    )
    install_process_singletons()
    return get_tool_registry()


# --------------------------------------------------------------------------- #
# 定义契约：直构 / 注册表 / 进程装配 三个入口交叉验证
# --------------------------------------------------------------------------- #


def test_definition_advertises_only_running_existing_teams() -> None:
    """(契约) 直构定义只声明「运行已存在 Team」，verb 不含 Prepare/配置/准备 口径。

    潜在缺陷：描述或展示标题回退到英文 Prepare/Configure 口径，再次让模型在
    「帮我配置个 team」时选中这个只会运行的工具。
    """

    definition = _definition()

    assert definition.name == TOOL_AGENT_TEAM == "agent_team"
    assert definition.group == TOOL_GROUP_AGENT_TEAM
    assert definition.execution_mode == "thread"
    assert definition.display is not None

    # 必须显式否定「创建 / 修改配置」的能力，并声明 Team 不存在时先让用户创建。
    assert "Run an existing Agent Team configuration" in definition.description
    assert "cannot create or edit Team configurations" in definition.description
    assert "if the Team does not exist, ask the user to create it first" in definition.description
    # 回归守卫：旧文案（把本工具说成「准备一份已配置 Team 的执行方案」）不得回归。
    assert "Prepare a configured Agent Team execution plan" not in definition.description

    verb = definition.display.verb
    assert isinstance(verb, str)
    assert verb != ""
    assert verb.strip() == verb
    assert verb == "运行已有 Agent Team"
    for forbidden in ("prepare", "configur", "配置", "准备", "创建"):
        assert forbidden not in verb.lower(), f"verb 不得出现误导词 {forbidden!r}: {verb!r}"


def test_model_facing_projection_carries_the_new_contract() -> None:
    """(契约) 模型可见投影（name/description/parameters）与定义同源。

    潜在缺陷：投影用 args_model 类名或旧描述，导致模型实际收到的仍是旧文案；
    或 team_id 入参说明不再提示「必须取自可用 Team 目录」。
    """

    definition = _definition()
    projection = definition.to_model_tool_definition()

    assert projection["name"] == "agent_team"
    assert projection["description"] == definition.description

    parameters = projection["parameters"]
    assert set(parameters["required"]) == {"team_id", "goal", "node_goals"}
    team_id_description = parameters["properties"]["team_id"]["description"]
    assert team_id_description
    assert "available agent teams catalog" in team_id_description.lower()


@pytest.mark.usefixtures("isolated_storage")
def test_registry_round_trip_preserves_description_and_verb(monkeypatch) -> None:
    """(交叉入口) 经 ``ToolRegistry.normalized()`` 注册后再读回，文案 / verb 不漂移。

    潜在缺陷：``normalized()`` 重建 ``ToolDefinition`` 时丢失 ``display`` 或改写
    ``description``，使注册表暴露给上层/前端的定义与 handler 直构的不一致。
    """

    monkeypatch.setattr(
        team_registry_module,
        "_REGISTRY",
        AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource()),
    )
    direct = build_agent_team_run_definition()
    registry = ToolRegistry([direct])

    registered = registry.get_tool_definition("agent_team")
    assert registered is not None
    assert registered.description == direct.description
    assert registered.display is not None
    assert registered.display.verb == direct.display.verb
    assert registered.display == direct.display
    assert registered.to_model_tool_definition() == direct.to_model_tool_definition()
    assert registry.get_schema("agent_team") == direct.to_model_tool_definition()["parameters"]


def test_assembled_tool_registry_exposes_identical_agent_team_definition(
    assembled_tool_registry,
) -> None:
    """(交叉入口) 工具注册表进程装配后的 agent_team 定义与直接构造完全一致。

    潜在缺陷：装配路径注册了另一个定义，或漏注册 agent_team / 装配顺序导致构建失败。
    """

    assembled = assembled_tool_registry.get_tool_definition("agent_team")
    assert assembled is not None

    direct = build_agent_team_run_definition()
    assert (assembled.name, assembled.group, assembled.description) == (
        direct.name,
        direct.group,
        direct.description,
    )
    assert assembled.display == direct.display
    assert assembled.execution_mode == direct.execution_mode
    assert "agent_team" in assembled_tool_registry.get_all_tool_names()
    # 同组的配置提案工具与执行工具必须能同时存在（职责边界：一个提案、一个运行）。
    assert (
        "propose_agent_team_configuration" in assembled_tool_registry.get_all_tool_names()
    )


# --------------------------------------------------------------------------- #
# execute()：执行上下文 / 准备分支
# --------------------------------------------------------------------------- #


def test_prepare_branch_reports_missing_team() -> None:
    """(契约) ``team_register.resolve`` 返回 None 时给出点名 Team 的失败说明。"""

    tool = _bare_tool()
    tool.team_register = SimpleNamespace(resolve=lambda scope, team_id: None)

    observation = tool.execute(
        team_id="ghost-team",
        goal="交付报告",
        node_goals=_node_inputs(develop='实现'),
        execution_context=_context(),
    )

    assert observation.status == "error"
    assert observation.content.startswith("The Team configuration does not exist: ghost-team")
    assert "ghost-team" in observation.content


def test_whitespace_team_id_is_rejected_at_the_args_boundary() -> None:
    """team_id 的 pattern 已排除空白：工具与 validator 不再各自 strip / 判空。"""

    with pytest.raises(ValidationError):
        AgentTeamArgs.model_validate(
            {
                "team_id": "   ",
                "goal": "交付报告",
                "node_goals": [{"node_id": "develop", "goal": "实现", "summary": "实现"}],
            }
        )


# --------------------------------------------------------------------------- #
# 入参边界
# --------------------------------------------------------------------------- #


def test_team_id_argument_rejects_blank_and_unknown_fields() -> None:
    """(边界) ``AgentTeamArgs`` 拒绝空 team_id / 空 node_goals / 未知字段。

    潜在缺陷：松散的参数模型接受空 team_id 或额外字段，把非法输入带到执行期。
    """

    with pytest.raises(ValidationError):
        AgentTeamArgs(team_id="", goal="goal", node_goals=_node_inputs(develop='实现'))

    with pytest.raises(ValidationError):
        AgentTeamArgs(team_id="code-quality", goal="goal", node_goals=_node_inputs())

    with pytest.raises(ValidationError):
        AgentTeamArgs(
            team_id="code-quality",
            goal="goal",
            node_goals=_node_inputs(develop='实现'),
            unexpected="x",  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        AgentTeamArgs(team_id="   ", goal="goal", node_goals=_node_inputs(develop="实现"))


# --------------------------------------------------------------------------- #
# 准备服务：成功路径与边界（暴露准备阶段内部装配缺陷）
# --------------------------------------------------------------------------- #


def test_prepare_branch_succeeds_for_an_existing_team(monkeypatch) -> None:
    """(契约) Team 已存在且输入合法时，准备分支落待确认 TeamRun（pending）并要求挂起。

    潜在缺陷：准备阶段内部抛异常被兜底成 ``agent_team_run_creation_invalid``，
    使工具在「Team 确实存在、目标合法」这条唯一主路径上永远无法成功。
    """

    child = _child_profile()
    _stub_preparation_lookups(monkeypatch, child)

    tool = _bare_tool()
    tool.team_register = SimpleNamespace(
        resolve=lambda scope, team_id: AgentTeamConfiguration.model_validate(_configuration())
    )
    tool.agent_team_preparation_service = AgentTeamPreparationService()

    captured: dict[str, object] = {}

    def create_pending_confirmation(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=101)

    tool.agent_team_run_approval = SimpleNamespace(
        create_pending_confirmation=create_pending_confirmation
    )

    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals=_node_inputs(develop='实现', review='审查'),
        execution_context=_context(),
    )

    assert observation.status == "success", (
        "已存在的 Team + 合法输入应产出 pending 观察，实际为 "
        f"content={observation.content!r}"
    )
    assert observation.display_data["team_run_id"] == 101
    assert observation.team_run_id == 101
    # 审批门由后端强制：工具只建 pending 并返回等待占位，决定与启动不经模型。
    assert observation.awaiting_decision is True
    assert observation.suspend is True
    assert captured["parent_run_id"] == 2
    assert captured["tool_call_id"] == observation.tool_call_id


def test_preparation_service_builds_preview_nodes_and_runtime_snapshots(monkeypatch) -> None:
    """(契约) ``prepare()`` 应产出预览节点清单（含 tools）与节点运行快照。

    潜在缺陷：预览节点装配引用了未定义的名字，准备阶段必然抛 ``NameError``，
    被工具层兜底成 creation_invalid，整体功能不可用。
    """

    child = _child_profile()
    _stub_preparation_lookups(monkeypatch, child)

    result = AgentTeamPreparationService().prepare(
        AgentTeamConfiguration.model_validate(_configuration()),
        goal="交付报告",
        node_goals=_node_goal_map(develop="实现", review="审查"),
        workspace_root="ws",
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        parent_agent=_runtime(child),
    )

    assert set(result.node_runtime_snapshots) == {"develop", "review"}
    assert [node["node_id"] for node in result.preview_fields["nodes"]] == ["develop", "review"]
    # 节点工具 = Profile 声明的工具减去禁用清单：这里刻意**不**补足委派 / 父子通信工具——它们属于
    # 节点 Agent Profile 自身的能力声明，Team 层只做剥离、不代为追加（见 materialize_node_tools）。
    assert result.preview_fields["nodes"][0]["tools"] == ["read_file"]
    assert result.preview_fields["goal"] == "交付报告"
    assert result.preview_fields["team_id"] == "code-quality"


def test_preparation_service_rejects_blank_goal(monkeypatch) -> None:
    """(边界) 纯空白总目标应被拒绝，且标记为可重试（模型修正后能再试）。

    潜在缺陷：空白目标被当成合法目标冻结进节点 system prompt。
    """

    child = _child_profile()
    _stub_preparation_lookups(monkeypatch, child)

    with pytest.raises(TeamToolError) as captured:
        AgentTeamPreparationService().prepare(
            AgentTeamConfiguration.model_validate(_configuration()),
            goal="   ",
            node_goals=_node_goal_map(develop="实现", review="审查"),
            workspace_root="ws",
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            parent_agent=_runtime(child),
        )

    assert captured.value.retryable is True
    assert "blank" in str(captured.value)


@pytest.mark.parametrize(
    ("node_goals", "expected"),
    [
        (_node_goal_map(develop="实现"), "must define every node"),
        (_node_goal_map(develop="实现", review="审查", ghost="多余"), "unknown nodes"),
    ],
)
def test_preparation_service_validates_node_goal_coverage(
    monkeypatch, node_goals: dict[str, AgentTeamNodeGoal], expected: str
) -> None:
    """(边界) node_goals 必须与节点集合一一对应。

    潜在缺陷：漏节点 / 多节点被放过，运行期才在节点输入处炸开。空白子目标在
    ``AgentTeamNodeGoal`` 构造边界即被拒绝，见下方用例。
    """

    child = _child_profile()
    _stub_preparation_lookups(monkeypatch, child)

    with pytest.raises(TeamToolError) as captured:
        AgentTeamPreparationService().prepare(
            AgentTeamConfiguration.model_validate(_configuration()),
            goal="交付报告",
            node_goals=node_goals,
            workspace_root="ws",
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            parent_agent=_runtime(child),
        )

    assert expected in str(captured.value)
    assert captured.value.retryable is True


@pytest.mark.parametrize(("goal", "summary"), [("   ", "实现"), ("实现", "   ")])
def test_node_goal_rejects_blank_goal_or_summary(goal: str, summary: str) -> None:
    """(边界) 空白子目标或摘要在节点目标值对象边界被拒绝，不会流入准备服务。

    潜在缺陷：空白子目标被当成节点 user input 冻结进运行快照。
    """

    with pytest.raises(ValidationError, match="must not be blank"):
        AgentTeamNodeGoal(goal=goal, summary=summary)


# --------------------------------------------------------------------------- #
# HITL 决定分支：批准即确认并后台运行，驳回即落定；TeamRun 已被处理时以失败结果收尾
# --------------------------------------------------------------------------- #


def test_agent_team_declares_execute_user_decision() -> None:
    assert _bare_tool().to_definition().user_decision == "execute"


class _DecisionRuns:
    """TeamRun service 替身：按工具调用找回 TeamRun，记录确认与驳回，并给出 Team 落定结论。"""

    def __init__(
            self, *, reject_end_reason: str = "user_rejected", confirm_error=None
    ) -> None:
        self.row = SimpleNamespace(id=101, parent_run_id=2, status="pending")
        self.reject_end_reason = reject_end_reason
        self.confirm_error = confirm_error
        # Coordinator.start 替身返回的落定快照。
        self.final = SimpleNamespace(id=101, status="completed", end_reason=None)
        self.confirmed: list[dict] = []

    def get_for_tool_call(self, task_id, tool_call_id):
        assert (task_id, tool_call_id) == (1, "call-team")
        return self.row

    def get(self, _team_run_id):
        return self.row

    def confirm(self, team_run_id, configuration, **kwargs):
        if self.confirm_error is not None:
            self.row.status = "cancelled"
            raise self.confirm_error
        self.confirmed.append({"id": team_run_id, "configuration": configuration, **kwargs})
        return SimpleNamespace(id=team_run_id, status="running")

    def reject_pending_confirmation(self, _team_run_id):
        status = "cancelled"
        return SimpleNamespace(status=status, end_reason=self.reject_end_reason)


def _decide(monkeypatch, runs: _DecisionRuns, decision: str, data=None):
    from app.core.tool.handler import agent_team_run as tool_module
    from app.core.workflow.inbox.task_message import DecisionBody

    started: list[int] = []

    def start(row, _loop):
        started.append(row.id)
        return runs.final

    monkeypatch.setattr(
        tool_module, "get_agent_team_coordinator", lambda: SimpleNamespace(start=start)
    )
    monkeypatch.setattr(
        "app.core.tool.handler.agent_team.agent_team_result.AgentTeamRunArchiveService",
        lambda: SimpleNamespace(
            existing_archive_path=lambda _id: ".cosir/agent-team-runs/101.json"
        ),
    )
    monkeypatch.setattr(
        tool_module,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(get_run=lambda run_id: SimpleNamespace(id=run_id)),
    )
    parent_agent = object()
    monkeypatch.setattr("app.core.run_driver.resolve_agent_config", lambda _run: parent_agent)
    tool = _bare_tool()
    tool.agent_team_run_approval = runs
    body = DecisionBody(
        tool_call_id="call-team",
        tool_name=TOOL_AGENT_TEAM,
        decision=decision,
        feedback="换个分工" if decision == "reject" else None,
        data=data,
    )
    context = dataclasses.replace(_context(), user_decision=body)
    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals=_node_inputs(develop="实现"),
        execution_context=context,
    )
    return observation, started, parent_agent


_APPROVED_PLAN = {
    "configuration": {"team_id": "code-quality"},
    "goal": "最终目标",
    "node_goals": {"develop": {"goal": "实现", "summary": "实现"}},
}


def test_approval_confirms_with_the_proposing_run_config_and_blocks_until_settled(
    monkeypatch,
) -> None:
    runs = _DecisionRuns()

    observation, started, parent_agent = _decide(monkeypatch, runs, "approve", _APPROVED_PLAN)

    assert observation.status == "success"
    # 阻塞运行：工具结果就是 Team 的落定结论，不再是「running」占位。
    payload = json.loads(observation.content)
    assert (payload["status"], payload["trace_file"]) == (
        "completed", ".cosir/agent-team-runs/101.json"
    )
    [confirmed] = runs.confirmed
    assert (confirmed["id"], confirmed["goal"]) == (101, "最终目标")
    # 节点回落配置取自提出方案的主 Run，而不是执行决定时的当前 Run。
    assert confirmed["parent_agent"] is parent_agent
    assert started == [101]
    assert observation.team_run_id == 101


def test_rejection_settles_the_team_run_without_starting(monkeypatch) -> None:
    observation, started, _ = _decide(monkeypatch, _DecisionRuns(), "reject")

    assert observation.status == "success"
    payload = json.loads(observation.content)
    assert (payload["status"], payload["feedback"]) == ("rejected", "换个分工")
    assert started == []


def test_rejection_after_the_plan_was_resolved_elsewhere_reports_it(monkeypatch) -> None:
    runs = _DecisionRuns(reject_end_reason="parent_cancelled")

    observation, started, _ = _decide(monkeypatch, runs, "reject")

    assert observation.status == "error"
    assert "already resolved" in observation.content
    assert started == []


def test_approval_after_the_plan_was_resolved_elsewhere_starts_nothing(monkeypatch) -> None:
    runs = _DecisionRuns(confirm_error=ValueError("该 Agent Team 方案已处理，不能再确认"))

    observation, started, _ = _decide(monkeypatch, runs, "approve", _APPROVED_PLAN)

    assert observation.status == "error"
    assert "already resolved" in observation.content
    assert started == []


def test_failed_team_reports_its_settled_status(monkeypatch) -> None:
    """Coordinator 把调度异常收敛成 failed 后返回落定快照：工具原样把结论交给模型。"""
    runs = _DecisionRuns()
    runs.final = SimpleNamespace(id=101, status="failed", end_reason="node_run_failed")

    observation, started, _ = _decide(monkeypatch, runs, "approve", _APPROVED_PLAN)

    assert started == [101]
    assert observation.status == "success"
    payload = json.loads(observation.content)
    assert (payload["status"], payload["end_reason"]) == ("failed", "node_run_failed")
