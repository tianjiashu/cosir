"""``agent_team`` 执行工具的契约、两阶段语义与边界对抗测试。

本文件只消费生产代码（``app/**`` 只读），不修改任何实现。测试从三类入口交叉验证定义：
直构 ``ToolDefinition`` / ``ToolRegistry`` 归一化后读回 / ``ToolSystem`` 进程装配后读回；
并覆盖 ``AgentTeamRunTool.execute()`` 的「准备（``user_decision is None``）」与
「启动（APPROVE）」两条分支、``team_register.resolve`` 返回 ``None`` 的失败契约、
确认分支的失败映射，以及准备服务的边界输入。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent_team import registry as team_registry_module
from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration_scope_source import AgentTeamConfigurationScopeSource
from app.agent_team.registry import AgentTeamConfigurationRegistry
from app.agent_team.team_tool_error import TeamToolError
from app.config.configuration import get_tool_system
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.model_settings import ModelSettings
from app.core.tools.schemas import (
    EXECUTING_DECISION_KINDS,
    ToolExecutionContext,
    UserDecision,
    UserDecisionKind,
)
from app.core.tools.schemas.tool_names import TOOL_AGENT_TEAM
from app.core.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies
from app.core.tools.tool_grouping import TOOL_GROUP_AGENT_TEAM
from app.core.tools.tool_handler.agent_team.agent_team_run import (
    AgentTeamRunTool,
    build_agent_team_run_definition,
)
from app.core.tools.tool_models import AgentTeamArgs
from app.core.tools.tool_registry import ToolRegistry
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.service.agent_team import agent_team_preparation_service as prep_module
from app.service.agent_team.agent_team_preparation_service import (
    AgentTeamPreparationService,
)


# --------------------------------------------------------------------------- #
# 夹具与构造助手
# --------------------------------------------------------------------------- #


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
                "statuses": ["done", "blocked"],
            },
            {
                "node_id": "review",
                "name": "审查",
                "agent_id": "general-assistant",
                "statuses": ["passed", "needs_changes"],
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


def _context(user_decision: UserDecision | None = None) -> ToolExecutionContext:
    """构造带运行时依赖的执行上下文。

    生产上下文的 ``runtime_dependencies.parent_agent_profile`` 由 runtime 注入本次 Run 的 per-run
    profile；工具依赖它准备节点模型设置，因此这里默认注入一份已物化的父 profile。需要验证
    「未注入」口径的用例直接自行构造 ``ToolExecutionContext``。
    """

    return ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path("C:/ws-root"),
        run_id=2,
        user_decision=user_decision,
        runtime_dependencies=ToolRuntimeDependencies(
            parent_agent_profile=_parent_run_profile()
        ),
    )


def _approve_decision(**data_overrides: object) -> UserDecision:
    data: dict[str, object] = {
        "configuration": _configuration(),
        "goal": "交付报告",
        "node_goals": {"develop": "实现", "review": "审查"},
    }
    data.update(data_overrides)
    return UserDecision(request_id="7", kind=UserDecisionKind.APPROVE, data=data)


def _child_profile() -> AgentProfile:
    """构造一个已物化模型设置的 CHILD profile，使准备阶段能通过模型配置解析。"""

    return AgentProfile(
        agent_id="general-assistant",
        role="general-assistant",
        description="通用子 Agent",
        system_prompt="child system prompt",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        model_settings=_materialized_model_settings(),
    )


def _materialized_model_settings() -> ModelSettings:
    """构造「已物化连接与能力字段」的模型运行设置。

    真实链路由 ``run.model_config_id`` 经 ``ModelSettings.from_model_config_record`` 物化；
    测试用等价的显式值，避免用例依赖数据库中的模型配置记录。
    """

    return ModelSettings(
        base_url="http://127.0.0.1/v1",
        api_key="secret",
        model_name="local-model",
        context_window_k=32,
        supports_thinking=True,
        supports_reasoning_effort=True,
        supports_image=False,
    )


def _parent_run_profile() -> AgentProfile:
    """构造本次 Run 由运行时注入的父 Agent profile（主 Agent + 已物化模型设置）。

    潜在缺陷：若调用方改用注册表共享单例（内置 profile 的 ``model_settings`` 只有偏好项、
    ``model_config_id`` 为空），节点模型设置回落会走 ``get_config(None)`` 抛 ``KeyError``。
    """

    return AgentProfile(
        agent_id="main_agent",
        role="main_agent",
        system_prompt="main system prompt",
        allowed_tools=["agent_team", "read_file"],
        agent_type=AgentProfileType.MAIN,
        model_config_id=1,
        model_settings=_materialized_model_settings(),
    )


def _stub_preparation_lookups(monkeypatch: pytest.MonkeyPatch, child: AgentProfile) -> None:
    """把准备服务对 Agent 注册表的查找替换为最小桩，保留装配逻辑本体。

    节点工具来自 Profile 的 ``allowed_tools``（不经工具系统解析 schema），因此这里只需桩掉
    节点 Profile 的解析来源。
    """

    registry = SimpleNamespace(resolve=lambda scope, agent_id: child)
    monkeypatch.setattr(prep_module, "get_agent_registry", lambda: registry)


@pytest.fixture
def assembled_tool_system(isolated_storage, install_process_singletons, monkeypatch):
    """装配真实 ``ToolSystem``：按 lifespan 的顺序先注入 Team 注册表，再构建单例。"""

    monkeypatch.setattr(
        team_registry_module,
        "_REGISTRY",
        AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource()),
    )
    install_process_singletons()
    return get_tool_system()


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
    assert definition.need_HIL is True
    assert definition.execution_mode == "thread"
    assert definition.display is not None

    # 必须显式否定「创建 / 修改配置」的能力，并声明 Team 必须已存在。
    assert "must already exist" in definition.description
    assert "cannot create or modify" in definition.description
    assert "Team configurations" in definition.description
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
    或 team_id 入参说明不再提示「必须是已存在的 Team」。
    """

    definition = _definition()
    projection = definition.to_model_tool_definition()

    assert projection["name"] == "agent_team"
    assert projection["description"] == definition.description

    parameters = projection["parameters"]
    assert set(parameters["required"]) == {"team_id", "goal", "node_goals"}
    team_id_description = parameters["properties"]["team_id"]["description"]
    assert team_id_description
    assert "existing" in team_id_description.lower()


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
    assert registered.need_HIL is True
    assert registered.to_model_tool_definition() == direct.to_model_tool_definition()
    assert registry.get_schema("agent_team") == direct.to_model_tool_definition()["parameters"]


def test_assembled_tool_system_exposes_identical_agent_team_definition(
    assembled_tool_system,
) -> None:
    """(交叉入口) ``ToolSystem`` 进程装配后的 agent_team 定义与直接构造完全一致。

    潜在缺陷：装配路径注册了另一个定义，或漏注册 agent_team / 装配顺序导致构建失败。
    """

    assembled = assembled_tool_system.registry.get_tool_definition("agent_team")
    assert assembled is not None

    direct = build_agent_team_run_definition()
    assert (assembled.name, assembled.group, assembled.description) == (
        direct.name,
        direct.group,
        direct.description,
    )
    assert assembled.display == direct.display
    assert assembled.need_HIL == direct.need_HIL
    assert assembled.execution_mode == direct.execution_mode
    assert "agent_team" in assembled_tool_system.registry.get_all_tool_names()
    # 同组的配置提案工具与执行工具必须能同时存在（职责边界：一个提案、一个运行）。
    assert (
        "propose_agent_team_configuration" in assembled_tool_system.registry.get_all_tool_names()
    )


# --------------------------------------------------------------------------- #
# execute()：执行上下文 / 准备分支 / 决定分流
# --------------------------------------------------------------------------- #


def test_execute_without_context_is_a_non_retryable_error() -> None:
    """(异常) 缺少执行上下文时返回 error 观察而非抛异常。

    潜在缺陷：``execution_context is None`` 时抛 ``AttributeError``，或把 retryable 置真。
    """

    observation = _bare_tool().execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=None,
    )

    assert observation.status == "error"
    assert observation.error == "agent_team requires an execution context."
    assert observation.retryable is False


def test_prepare_branch_reports_missing_team_as_non_retryable_creation_invalid() -> None:
    """(契约) ``team_register.resolve`` 返回 None 时给出不可重试的 ``agent_team_run_creation_invalid``。

    潜在缺陷：Team 不存在被映射为可重试错误，模型会反复重试一个永远不存在的 Team；
    或错误码与 retryable 与契约不符。
    """

    tool = _bare_tool()
    tool.team_register = SimpleNamespace(resolve=lambda scope, team_id: None)

    observation = tool.execute(
        team_id="ghost-team",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=_context(),
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_run_creation_invalid"
    assert observation.retryable is False
    assert "ghost-team" in (observation.reason or "")


def test_prepare_branch_strips_team_id_before_resolving() -> None:
    """(边界) team_id 首尾空白必须先 strip 再解析，且解析作用域为 workspace 根。

    潜在缺陷：未 strip 导致配置明明存在却解析失败（键不匹配）。
    """

    tool = _bare_tool()
    seen: dict[str, str] = {}

    def resolve(scope: str, team_id: str):
        seen["scope"], seen["team_id"] = scope, team_id
        return None

    tool.team_register = SimpleNamespace(resolve=resolve)
    context = _context()

    tool.execute(
        team_id="   code-quality   ",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=context,
    )

    assert seen["team_id"] == "code-quality"
    assert seen["scope"] == str(context.workspace_root)


def test_whitespace_only_team_id_resolves_to_the_empty_string() -> None:
    """(对抗) 纯空白 team_id 会被 strip 成空串去解析，最终以 creation_invalid 收场。

    潜在缺陷：把空串当成合法 team_id 传下去，命中某个默认 / 空配置。
    """

    tool = _bare_tool()
    seen: list[str] = []
    tool.team_register = SimpleNamespace(resolve=lambda scope, team_id: seen.append(team_id))

    observation = tool.execute(
        team_id="    ",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=_context(),
    )

    assert seen == [""]
    assert observation.status == "error"
    assert observation.error == "agent_team_run_creation_invalid"
    assert observation.retryable is False


def test_decision_kinds_are_exposed_through_a_module_level_enum() -> None:
    """(契约) 决定种类是模块级 ``UserDecisionKind``，执行判据收口在 ``EXECUTING_DECISION_KINDS``。

    潜在缺陷：调用方臆造 ``UserDecision.Kind`` 这类不存在的嵌套命名空间，导致访问即抛
    ``AttributeError``（见下方 execute 分流用例）。
    """

    assert not hasattr(UserDecision, "Kind")
    assert UserDecision.model_fields["kind"].annotation is UserDecisionKind
    assert EXECUTING_DECISION_KINDS == frozenset({UserDecisionKind.APPROVE})


def test_approve_decision_routes_to_start_branch_only() -> None:
    """(两阶段契约) APPROVE 必须走启动分支：调用 ``confirm_and_start`` 并返回 running 观察。

    潜在缺陷：分流条件引用不存在的 ``UserDecision.Kind``，任何非空决定都抛
    ``AttributeError``，使「用户批准 → 启动 Team」这一必经路径彻底不可达；或启动分支不再
    转发运行时注入的父 profile，使确认边界重新从注册表解析出无模型连接的单例。
    """

    tool = _bare_tool()
    calls: list[tuple] = []
    row = SimpleNamespace(id=7, status=AgentTeamRunStatus.RUNNING.value, team_id="code-quality")

    def confirm_and_start(team_run_id, configuration, *, goal, node_goals, parent_agent_profile):
        calls.append((team_run_id, configuration, goal, node_goals, parent_agent_profile))
        return row

    tool.agent_team_run_service = SimpleNamespace(confirm_and_start=confirm_and_start)
    tool.team_register = SimpleNamespace(resolve=lambda *a: pytest.fail("准备分支不应被触达"))
    context = _context(_approve_decision())

    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现", "review": "审查"},
        execution_context=context,
    )

    assert observation.status == "success"
    assert json.loads(observation.content) == {"status": "running", "team_id": "code-quality"}
    assert observation.display_data["kind"] == "agent-team-preview"
    assert observation.display_data["status"] == "running"
    # 启动分支不得再向工作流下发待决请求，否则会形成无法结束的挂起。
    assert observation.user_input_request is None
    assert calls == [
        (
            7,
            _configuration(),
            "交付报告",
            {"develop": "实现", "review": "审查"},
            context.runtime_dependencies.parent_agent_profile,
        )
    ]


def test_execute_reports_runtime_unavailable_without_injected_parent_profile() -> None:
    """(异常/装配契约) 运行时未注入父 profile 时给出专用不可重试错误，不进入准备分支。

    潜在缺陷：把「依赖未注入」当成输入问题继续执行，最终以无信息的
    ``agent_team_run_creation_invalid`` 收场，掩盖真正的装配缺失。
    """

    tool = _bare_tool()
    # 刻意不经 ``_context``：这里要构造「runtime_dependencies 为空」的真实缺失形态。
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=1,
        workspace_root=Path("C:/ws-root"),
        run_id=2,
    )
    tool.team_register = SimpleNamespace(
        resolve=lambda *a: pytest.fail("未注入父 profile 时不得进入准备分支")
    )

    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=context,
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_runtime_unavailable"
    assert observation.retryable is False


@pytest.mark.parametrize("kind", [UserDecisionKind.REJECT, UserDecisionKind.ABORT])
def test_non_approve_decision_never_starts_a_run(kind: UserDecisionKind) -> None:
    """(对抗) 只有 APPROVE 才进入启动分支；驳回 / 放弃即使被重放也不启动 Coordinator。

    潜在缺陷：分流判据写成「决定非空」，让驳回 / 放弃也真正启动 Team；或分流本身抛异常。
    """

    tool = _bare_tool()
    started: list[tuple] = []
    tool.agent_team_run_service = SimpleNamespace(
        confirm_and_start=lambda *a, **k: started.append(a)
    )
    tool.team_register = SimpleNamespace(resolve=lambda scope, team_id: None)

    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现"},
        execution_context=_context(
            UserDecision(request_id="7", kind=kind, data={"configuration": _configuration()})
        ),
    )

    assert started == []
    assert observation.status == "error"
    assert observation.error == "agent_team_run_creation_invalid"


# --------------------------------------------------------------------------- #
# 第二遍实现 _start_confirmed_run：失败映射与终态语义
#
# 说明：以下用例直接调用第二遍实现，把「分流」（上面 execute 用例）与「确认分支自身的失败
# 映射」解耦，便于分别定位缺陷。
# --------------------------------------------------------------------------- #


def _start_branch_tool(confirm_and_start) -> AgentTeamRunTool:
    tool = _bare_tool()
    tool.agent_team_run_service = SimpleNamespace(confirm_and_start=confirm_and_start)
    return tool


def test_confirmed_start_returns_running_observation() -> None:
    """(契约) 成功启动时返回 running 观察与运行卡片数据，且不下发待决请求。"""

    row = SimpleNamespace(id=11, status=AgentTeamRunStatus.RUNNING.value, team_id="code-quality")
    tool = _start_branch_tool(lambda *a, **k: row)

    observation = tool._start_confirmed_run(_approve_decision(), _parent_run_profile())

    assert observation.status == "success"
    assert json.loads(observation.content) == {"status": "running", "team_id": "code-quality"}
    assert observation.display_data["team_run_id"] == 11
    assert observation.display_data["status"] == "running"
    assert observation.user_input_request is None


def test_confirmed_start_rejects_non_numeric_request_id() -> None:
    """(异常) request_id 非整数时归一化为不可重试的确认错误，不调用启动服务。

    潜在缺陷：``int()`` 包装失败直接抛出，工具层不再返回观察。
    """

    tool = _start_branch_tool(lambda *a, **k: pytest.fail("非法 request_id 不得触达启动服务"))
    decision = UserDecision(request_id="not-a-number", kind=UserDecisionKind.APPROVE, data={})

    observation = tool._start_confirmed_run(decision, _parent_run_profile())

    assert observation.status == "error"
    assert observation.error == "agent_team_confirmation_invalid"
    assert observation.retryable is False
    assert "Invalid TeamRun identifier" in (observation.reason or "")


def test_confirmed_start_rejects_malformed_payload() -> None:
    """(异常) 确认载荷缺字段时给出确认错误，不调用启动服务。

    潜在缺陷：跳过形状校验，把半成品载荷透传给 ``confirm_and_start``。
    """

    tool = _start_branch_tool(lambda *a, **k: pytest.fail("非法载荷不得触达启动服务"))
    decision = UserDecision(
        request_id="7", kind=UserDecisionKind.APPROVE, data={"goal": "交付报告"}
    )

    observation = tool._start_confirmed_run(decision, _parent_run_profile())

    assert observation.status == "error"
    assert observation.error == "agent_team_confirmation_invalid"
    assert observation.retryable is False
    assert "Invalid confirmation input" in (observation.reason or "")


def test_confirmed_start_maps_missing_pending_run_to_error() -> None:
    """(异常) 待确认 TeamRun 不存在（``KeyError``）应映射为确认错误。

    潜在缺陷：``KeyError`` 逃逸出工具层。
    """

    def confirm_and_start(*args, **kwargs):
        raise KeyError(7)

    observation = _start_branch_tool(confirm_and_start)._start_confirmed_run(
        _approve_decision(), _parent_run_profile()
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_confirmation_invalid"
    assert observation.retryable is False
    assert "does not exist" in (observation.reason or "")


def test_confirmed_start_surfaces_domain_value_error() -> None:
    """(异常) 领域校验失败（``ValueError``）应把原消息透出到 reason。

    潜在缺陷：领域错误被吞掉，只留一句无信息量的「启动失败」。
    """

    def confirm_and_start(*args, **kwargs):
        raise ValueError("确认配置的 team_id 与待确认 Team 不一致")

    observation = _start_branch_tool(confirm_and_start)._start_confirmed_run(
        _approve_decision(), _parent_run_profile()
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_confirmation_invalid"
    assert "team_id" in (observation.reason or "")


@pytest.mark.parametrize(
    "terminal_status",
    [AgentTeamRunStatus.CANCELLED.value, AgentTeamRunStatus.FAILED.value],
)
def test_confirmed_start_does_not_report_success_for_terminal_runs(terminal_status: str) -> None:
    """(对抗) 条件迁移未生效且行已终态时不得报告成功。

    潜在缺陷：把 cancelled/failed 的 TeamRun 当成功返回，模型会等一个永远不会运行的 Team。
    """

    row = SimpleNamespace(id=7, status=terminal_status, team_id="code-quality")
    observation = _start_branch_tool(lambda *a, **k: row)._start_confirmed_run(
        _approve_decision(), _parent_run_profile()
    )

    assert observation.status == "error"
    assert observation.error == "agent_team_confirmation_invalid"
    assert terminal_status in (observation.reason or "")


# --------------------------------------------------------------------------- #
# 入参边界
# --------------------------------------------------------------------------- #


def test_team_id_argument_rejects_blank_and_unknown_fields() -> None:
    """(边界) ``AgentTeamArgs`` 拒绝空 team_id / 空 node_goals / 未知字段。

    潜在缺陷：松散的参数模型接受空 team_id 或额外字段，把非法输入带到执行期。
    """

    with pytest.raises(ValidationError):
        AgentTeamArgs(team_id="", goal="goal", node_goals={"develop": "实现"})

    with pytest.raises(ValidationError):
        AgentTeamArgs(team_id="code-quality", goal="goal", node_goals={})

    with pytest.raises(ValidationError):
        AgentTeamArgs(
            team_id="code-quality",
            goal="goal",
            node_goals={"develop": "实现"},
            unexpected="x",  # type: ignore[call-arg]
        )

    # 纯空白 team_id 在参数层被接受（长度非零），执行层 strip 后落空——记录该边界事实。
    args = AgentTeamArgs(team_id="   ", goal="goal", node_goals={"develop": "实现"})
    assert args.team_id.strip() == ""


# --------------------------------------------------------------------------- #
# 准备服务：成功路径与边界（暴露准备阶段内部装配缺陷）
# --------------------------------------------------------------------------- #


def test_prepare_branch_succeeds_for_an_existing_team(monkeypatch) -> None:
    """(两阶段契约) Team 已存在且输入合法时，准备分支应成功落待确认 TeamRun（pending）。

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

    tool.agent_team_run_service = SimpleNamespace(
        create_pending_confirmation=create_pending_confirmation
    )

    observation = tool.execute(
        team_id="code-quality",
        goal="交付报告",
        node_goals={"develop": "实现", "review": "审查"},
        execution_context=_context(),
    )

    assert observation.status == "success", (
        "已存在的 Team + 合法输入应产出 pending 观察，实际为 "
        f"error={observation.error!r} reason={observation.reason!r}"
    )
    assert json.loads(observation.content) == {"status": "pending", "team_id": "code-quality"}
    assert observation.display_data["team_run_id"] == 101
    assert observation.display_data["status"] == "pending"
    assert observation.user_input_request is not None
    assert observation.user_input_request.request_id == "101"
    assert captured["parent_run_id"] == 2


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
        node_goals={"develop": "实现", "review": "审查"},
        workspace_root="ws",
        parent_task_id=1,
        parent_run_id=2,
        workspace_id=3,
        parent_agent_profile=child,
    )

    assert set(result.node_runtime_snapshots) == {"develop", "review"}
    assert [node["node_id"] for node in result.preview_fields["nodes"]] == ["develop", "review"]
    assert result.preview_fields["nodes"][0]["tools"] == [
        "read_file",
        "delegate_task",
        "child_agent_send",
        "child_agent_status",
        "child_agent_wait",
    ]
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
            node_goals={"develop": "实现", "review": "审查"},
            workspace_root="ws",
            parent_task_id=1,
            parent_run_id=2,
            workspace_id=3,
            parent_agent_profile=child,
        )

    assert captured.value.retryable is True
    assert "blank" in str(captured.value)


@pytest.mark.parametrize(
    ("node_goals", "expected"),
    [
        ({"develop": "实现"}, "must define every node"),
        ({"develop": "实现", "review": "审查", "ghost": "多余"}, "unknown nodes"),
        ({"develop": "   ", "review": "审查"}, "must not be blank"),
    ],
)
def test_preparation_service_validates_node_goal_coverage(
    monkeypatch, node_goals: dict[str, str], expected: str
) -> None:
    """(边界) node_goals 必须与节点集合一一对应且非空白。

    潜在缺陷：漏节点 / 多节点 / 空白子目标被放过，运行期才在节点输入处炸开。
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
            parent_agent_profile=child,
        )

    assert expected in str(captured.value)
    assert captured.value.retryable is True
