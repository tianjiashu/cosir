"""Agent Team 静态契约和持久化运行意图测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration.team_transition_definition import TeamTransitionDefinition
from app.agent_team.registry import AgentTeamConfigurationRegistry
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.api.schemas.request.confirm_agent_team_request import ConfirmAgentTeamRequest
from app.core.tools.tool_handler.agent_team.propose_agent_team_configuration import (
    ProposeAgentTeamConfigurationTool,
)
from app.core.tools.tool_models.agent_team.propose_agent_team_configuration_args import (
    ProposeAgentTeamConfigurationArgs,
)
from app.service.configuration.agent_team_configuration_service import (
    AgentTeamConfigurationService,
)
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud
from app.storage.engine_cache import create_sqlite_engine
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.task_model import TaskModel
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import initialize_app_schema


def _configuration(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "team_id": "code-quality",
        "name": "代码质量 Team",
        "description": "开发、审查和测试",
        "scope": "workspace",
        "nodes": [
            {
                "node_id": "develop",
                "name": "开发",
                "agent_id": "general-assistant",
                "node_type": "start",
                "statuses": ["done", "blocked"],
            },
            {
                "node_id": "review",
                "name": "审查",
                "agent_id": "general-assistant",
                "node_type": "middle",
                "statuses": ["passed", "needs_changes"],
            },
            {
                "node_id": "finish",
                "name": "完成",
                "agent_id": "general-assistant",
                "node_type": "end",
                "statuses": ["passed"],
            },
        ],
        "transitions": [
            {"from_node_id": "develop", "status": "done", "target_node_id": "review"},
            {"from_node_id": "develop", "status": "blocked", "target_node_id": "finish"},
            {"from_node_id": "review", "status": "passed", "target_node_id": "finish"},
            {
                "from_node_id": "review",
                "status": "needs_changes",
                "target_node_id": "develop",
            },
        ],
    }
    document.update(overrides)
    return document


def test_configuration_validates_status_transitions_and_loops() -> None:
    configuration = AgentTeamConfiguration.model_validate(_configuration())

    assert configuration.start_node_id == "develop"
    assert configuration.transitions_for("review", "needs_changes")[0].target_node_id == "develop"


def test_proposal_uses_node_types_without_entry_or_transition_kind() -> None:
    proposal_input = _configuration()
    proposal_input.pop("scope")
    proposal = ProposeAgentTeamConfigurationArgs.model_validate(proposal_input)

    document = proposal.model_dump()
    assert "scope" not in document
    assert "entry_node_id" not in document
    assert all("transition_kind" not in transition for transition in document["transitions"])
    assert {node["node_type"] for node in document["nodes"]} == {
        "start",
        "middle",
        "end",
    }

    with pytest.raises(ValidationError):
        ProposeAgentTeamConfigurationArgs.model_validate({**document, "entry_node_id": "develop"})
    with pytest.raises(ValidationError):
        ProposeAgentTeamConfigurationArgs.model_validate({**document, "scope": "workspace"})


@pytest.mark.parametrize(
    ("field", "value"),
    (("node_id", "review node"), ("statuses", ["PASS", "fail"])),
)
def test_configuration_rejects_invalid_identifier_and_status_formats(
    field: str, value: object
) -> None:
    document = _configuration()
    document["nodes"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        AgentTeamConfiguration.model_validate(document)


def test_proposal_tool_separates_argument_and_configuration_validation() -> None:
    proposal = _configuration()
    proposal.pop("scope")

    observation = ProposeAgentTeamConfigurationTool().execute(**proposal)

    assert observation.status == "success"
    assert observation.display_data["scope"] == "workspace"

    rejected = ProposeAgentTeamConfigurationTool().execute(**{**proposal, "scope": "system"})
    assert rejected.status == "error"


def test_confirm_request_carries_final_configuration_instead_of_preview_fingerprint() -> None:
    """确认请求提交最终配置，旧的预览指纹协议不再属于接口契约。"""

    configuration = _configuration()
    payload = ConfirmAgentTeamRequest.model_validate(
        {
            "team_run_id": 123,
            "configuration": configuration,
        }
    )

    assert payload.team_run_id == 123
    assert payload.configuration == configuration
    with pytest.raises(ValidationError):
        ConfirmAgentTeamRequest.model_validate(
            {
                "team_run_id": 123,
                "configuration": configuration,
                "preview_fingerprint": "f" * 64,
            }
        )


def test_proposal_tool_does_not_construct_persisted_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提案工具只使用 Args 校验，不提前构造持久化配置模型。"""

    proposal = _configuration()
    proposal.pop("scope")

    def fail_domain_validation(cls: type[AgentTeamConfiguration], document: object) -> object:
        raise AssertionError("proposal tool must not validate persisted configuration")

    monkeypatch.setattr(
        AgentTeamConfiguration,
        "model_validate",
        classmethod(fail_domain_validation),
    )

    observation = ProposeAgentTeamConfigurationTool().execute(**proposal)

    assert observation.status == "success"
    assert observation.display_data["scope"] == "workspace"


def test_proposal_args_owns_graph_validation() -> None:
    """提案参数模型可以独立拒绝无效转移，不依赖持久化配置模型。"""

    proposal = _configuration(
        transitions=[
            {
                "from_node_id": "develop",
                "status": "done",
                "target_node_id": "missing",
            }
        ]
    )
    proposal.pop("scope")

    with pytest.raises(ValidationError, match="unknown target"):
        ProposeAgentTeamConfigurationArgs.model_validate(proposal)


def test_configuration_service_rejects_unavailable_node_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """确认保存由配置 service 负责校验节点引用，而不是由 API 拼装规则。"""

    class _EmptyAgentRegistry:
        def resolve(self, _scope: str, _agent_id: str) -> None:
            return None

    monkeypatch.setattr(
        "app.config.configuration.get_agent_registry",
        lambda: _EmptyAgentRegistry(),
    )
    proposal = _configuration()
    proposal.pop("scope")

    with pytest.raises(ValueError, match="child Agent"):
        AgentTeamConfigurationService().save_confirmed(
            proposal,
            scope="workspace",
            workspace_root=tmp_path,
        )


def test_configuration_rejects_unknown_transition_target() -> None:
    with pytest.raises(ValidationError, match="unknown target"):
        AgentTeamConfiguration.model_validate(
            _configuration(
                transitions=[
                    {
                        "from_node_id": "develop",
                        "status": "done",
                        "target_node_id": "missing",
                    }
                ]
            )
        )


def test_configuration_rejects_duplicate_end_nodes() -> None:
    nodes = list(_configuration()["nodes"])
    nodes.append(
        {
            "node_id": "finish-again",
            "name": "重复完成",
            "agent_id": "general-assistant",
            "node_type": "end",
            "statuses": ["passed"],
        }
    )

    with pytest.raises(ValidationError, match="exactly one end"):
        AgentTeamConfiguration.model_validate(_configuration(nodes=nodes))


def test_configuration_rejects_unreachable_node() -> None:
    nodes = list(_configuration()["nodes"])
    nodes.append(
        {
            "node_id": "orphan",
            "name": "孤立节点",
            "agent_id": "general-assistant",
            "node_type": "middle",
            "statuses": ["loop"],
        }
    )
    transitions = list(_configuration()["transitions"])
    transitions.append({"from_node_id": "orphan", "status": "loop", "target_node_id": "orphan"})

    with pytest.raises(ValidationError, match="unreachable"):
        AgentTeamConfiguration.model_validate(_configuration(nodes=nodes, transitions=transitions))


def test_configuration_rejects_branch_that_cannot_reach_end() -> None:
    nodes = list(_configuration()["nodes"])
    nodes.append(
        {
            "node_id": "dead-end",
            "name": "无法结束",
            "agent_id": "general-assistant",
            "node_type": "middle",
            "statuses": ["loop"],
        }
    )
    transitions = list(_configuration()["transitions"])
    transitions[3] = {
        "from_node_id": "review",
        "status": "needs_changes",
        "target_node_id": "dead-end",
    }
    transitions.extend(
        [
            {
                "from_node_id": "dead-end",
                "status": "loop",
                "target_node_id": "dead-end",
            },
        ]
    )

    with pytest.raises(ValidationError, match="cannot reach the end"):
        AgentTeamConfiguration.model_validate(_configuration(nodes=nodes, transitions=transitions))


def test_configuration_has_no_version_field() -> None:
    with pytest.raises(ValidationError):
        AgentTeamConfiguration.model_validate({**_configuration(), "version": 1})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("limits", {}),
        ("instruction", "不应写入 Team 配置"),
        ("max_attempts", 2),
        ("timeout_seconds", 60),
    ),
)
def test_configuration_rejects_removed_node_and_team_fields(field: str, value: object) -> None:
    document = _configuration()
    if field == "limits":
        document[field] = value
    else:
        document["nodes"][0][field] = value  # type: ignore[index]

    with pytest.raises(ValidationError):
        AgentTeamConfiguration.model_validate(document)


def test_transition_rejects_removed_priority_field() -> None:
    with pytest.raises(ValidationError):
        TeamTransitionDefinition(
            from_node_id="review",
            status="passed",
            target_node_id="finish",
            transition_kind="exclusive",
            priority=10,
        )


def test_configuration_rejects_parallel_transition_shape() -> None:
    with pytest.raises(ValidationError):
        AgentTeamConfiguration.model_validate(
            _configuration(
                transitions=[
                    {
                        "from_node_id": "develop",
                        "status": "done",
                        "target_node_ids": ["review"],
                    },
                    {
                        "from_node_id": "review",
                        "status": "passed",
                        "target_node_id": "finish",
                    },
                ]
            )
        )


def test_configuration_rejects_removed_join_fields() -> None:
    with pytest.raises(ValidationError):
        AgentTeamConfiguration.model_validate(
            _configuration(
                nodes=[
                    {
                        "node_id": "develop",
                        "name": "开发",
                        "agent_id": "general-assistant",
                        "node_type": "start",
                        "statuses": ["done", "join"],
                        "join_policy": "all",
                    },
                    {
                        "node_id": "review",
                        "name": "审查",
                        "agent_id": "general-assistant",
                        "node_type": "middle",
                        "statuses": ["passed", "needs_changes"],
                    },
                    {
                        "node_id": "finish",
                        "name": "完成",
                        "agent_id": "general-assistant",
                        "node_type": "end",
                        "statuses": ["passed"],
                    },
                ]
            )
        )


def test_registry_saves_and_reloads_workspace_configuration(tmp_path: Path) -> None:
    registry = AgentTeamConfigurationRegistry()
    configuration = AgentTeamConfiguration.model_validate(_configuration())
    AgentTeamConfigurationService(registry=registry).save(configuration, workspace_root=tmp_path)

    restored = AgentTeamConfigurationRegistry()
    AgentTeamConfigurationService(registry=restored).load_directory(
        str(tmp_path),
        tmp_path / ".cosir" / "agent-teams",
    )
    assert restored.resolve(tmp_path, "code-quality").team_id == "code-quality"


def test_agent_team_run_table_persists_confirmation_state(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)

    assert "agent_team_execution_requests" not in inspect(engine).get_table_names()
    table = inspect(engine).get_columns("agent_team_runs")
    columns = {item["name"] for item in table}

    assert {"id", "status", "configuration_snapshot_json", "state_json"} <= columns
    assert "preview_document_json" not in columns
    assert not {
        "team_run_id",
        "expires_at",
        "resolved_at",
        "generation",
        "current_node_id",
        "current_node_status",
        "current_node_output",
        "failure_kind",
        "failure_message",
    }.intersection(columns)
    assert "end_reason" in columns


def test_team_run_crud_round_trips_persisted_confirmation_state(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as session:
        workspace = WorkspaceModel(name="workspace", root_path=str(tmp_path))
        session.add(workspace)
        session.flush()
        task = TaskModel(workspace_id=workspace.id, title="parent", tool_definitions=[])
        session.add(task)
        session.flush()
        run = ConversationRunModel(
            task_id=task.id,
            status="waiting_for_input",
        )
        session.add(run)
        session.flush()

    crud = AgentTeamRunCrud.__new__(AgentTeamRunCrud)
    crud._session_factory = factory
    configuration = AgentTeamConfiguration.model_validate(_configuration())
    created = crud.create(
        configuration=configuration,
        workspace_id=workspace.id,
        parent_task_id=task.id,
        parent_run_id=run.id,
        preview_fingerprint="f" * 64,
        goal="检查代码",
        node_runtime_snapshots={"develop": {"agent_id": "general-assistant"}},
    )

    restored = crud.get_by_id(created.id)
    assert restored is not None
    assert restored.status == "pending"
    assert "requires_confirmation" not in restored.state_json
    assert restored.configuration_snapshot_json["team_id"] == "code-quality"
    assert crud.list_active() == []


def test_team_run_crud_updates_status_only_from_allowed_states(tmp_path: Path) -> None:
    """TeamRun 条件状态更新不能覆盖已经迁移后的状态。"""

    engine = create_sqlite_engine(tmp_path / "storage" / "app.sqlite3")
    initialize_app_schema(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as session:
        workspace = WorkspaceModel(name="workspace", root_path=str(tmp_path))
        session.add(workspace)
        session.flush()
        task = TaskModel(workspace_id=workspace.id, title="parent", tool_definitions=[])
        session.add(task)
        session.flush()
        run = ConversationRunModel(
            task_id=task.id,
            status="waiting_for_input",
        )
        session.add(run)
        session.flush()

    crud = AgentTeamRunCrud.__new__(AgentTeamRunCrud)
    crud._session_factory = factory
    created = crud.create(
        configuration=AgentTeamConfiguration.model_validate(_configuration()),
        workspace_id=workspace.id,
        parent_task_id=task.id,
        parent_run_id=run.id,
        preview_fingerprint="f" * 64,
        goal="检查代码",
        node_runtime_snapshots={"develop": {"agent_id": "general-assistant"}},
    )

    updated = crud.update_status_if_in(
        created.id,
        "running",
        ("pending",),
        state=AgentTeamRunState.initial({"develop": {"agent_id": "general-assistant"}}),
        started=True,
    )
    assert updated is not None
    assert updated.status == "running"
    assert "requires_confirmation" not in updated.state_json
    assert updated.end_reason is None
    assert [row.id for row in crud.list_active()] == [created.id]
    assert (
        crud.update_status_if_in(
            created.id,
            "cancelled",
            ("pending",),
        )
        is None
    )
