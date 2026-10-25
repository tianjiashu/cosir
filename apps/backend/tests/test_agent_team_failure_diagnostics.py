"""失败的 Agent Team 仍可定位，并能读取节点诊断。"""

import json
from types import SimpleNamespace

from app.service.agent_team.coordinator import AgentTeamCoordinator
from app.models.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
)
from app.api.schemas.response.agent_team_run_response import AgentTeamRunResponse
from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.events import ToolCallStatusChangedEvent
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import ToolMessageMetadata
from app.service.agent_team import agent_team_execution_timeline
from app.service.agent_team import agent_team_run_service as run_service_module
from app.service.agent_team.agent_team_execution_timeline import project_node_visits
from app.service.agent_team.agent_team_run_archive_service import AgentTeamRunArchiveService
from app.service.agent_team.agent_team_run_service import AgentTeamRunService


def test_failed_tool_event_keeps_team_run_locator_outside_display_data() -> None:
    """错误提示覆盖展示数据时，持久化运行引用仍须留在独立字段。"""

    state = {
        "messages": [
            {
                "id": "assistant-run-3",
                "role": "assistant",
                "runId": 3,
                "outcome": None,
                "parts": [
                    {
                        "type": "tool-call",
                        "toolCallId": "call-1",
                        "toolName": "agent_team",
                        "status": "running",
                        "presentation": {},
                        "display_data": {"kind": "agent-team-approval", "team_run_id": 17},
                    }
                ],
            }
        ],
        "activity": {
            "runId": 3,
            "status": "running",
            "endReason": None,
            "error": None,
            "usage": None,
            "contextWindowTotal": None,
            "launch": {"modelConfigId": None, "reasoningEffort": None, "banTools": []},
        },
        "error": None,
    }
    event = ToolCallStatusChangedEvent(
        task_id=2,
        run_id=3,
        tool_call_id="call-1",
        status="failed",
        error="执行失败",
        display_data={"status_hint": "执行失败"},
        team_run_id=17,
    )

    for mutation in plan_event(event, state):
        target = state
        for key in mutation.path[:-1]:
            target = target[key]
        target[mutation.path[-1]] = mutation.value

    part = state["messages"][0]["parts"][0]
    assert part["status"] == "failed"
    assert part["display_data"] == {"status_hint": "执行失败"}
    assert part["team_run_id"] == 17
    validate_snapshot(state)


def test_failed_node_visit_exposes_status_without_error_body_or_runtime_snapshot(
    monkeypatch,
) -> None:
    """节点读模型只投影状态；错误正文留在归档，不扩展有限 UI 契约。"""

    monkeypatch.setattr(
        agent_team_execution_timeline,
        "get_agent_profile_registry",
        lambda: SimpleNamespace(resolve=lambda *_args: SimpleNamespace(role="开发")),
    )
    state = AgentTeamRunState(
        node_executions={
            "implement": AgentTeamNodeExecution(
                node_id="implement",
                task_id=8,
                conversation_run_ids=[11],
            )
        },
        current_node_id="implement",
        node_runtime={
            "implement": AgentTeamRunRuntime(
                agent_id="developer",
                node_goal_summary="实现",
            )
        },
    )
    run = SimpleNamespace(
        id=11,
        created_at="2026-10-18T00:00:00",
        status="failed",
        end_reason="model_resolve_failed",
        final_output=None,
        agent_id="developer",
        error={
            "code": "model_resolve_failed",
            "message": "模型配置不可用，请检查模型设置",
        },
    )

    visits = project_node_visits(
        state=state,
        configuration={
            "nodes": [
                {
                    "node_id": "implement",
                    "name": "实现",
                    "agent_id": "developer",
                    "statuses": [],
                }
            ]
        },
        team_status="failed",
        run_records={11: run},
        child_tasks_by_parent={},
        workspace="test",
    )

    assert visits["implement"][0]["status"] == "failed"
    assert "failure_message" not in visits["implement"][0]
    assert "node_runtime" not in visits["implement"][0]

    archive = AgentTeamRunArchiveService.__new__(AgentTeamRunArchiveService)
    archive._conversation_run_crud = SimpleNamespace(get_many=lambda _ids: {11: run})
    document = archive._build_document(
        SimpleNamespace(
            state_json=state.to_json(),
            configuration_snapshot_json={
                "name": "开发团队",
                "nodes": [{"node_id": "implement", "name": "实现"}],
            },
            status="failed",
            end_reason="node_run_failed",
            goal_input="实现功能",
        )
    )
    assert document["failure"] == {"node": "实现", "reason": "模型配置不可用，请检查模型设置"}
    assert document["history"][0]["status"] == "failed"


def test_cold_rebuild_keeps_failed_team_run_locator(monkeypatch) -> None:
    """刷新页面后从 ToolMessage 元数据恢复定位信息，不依赖进程内快照。"""

    monkeypatch.setattr(
        ConversationTaskStateRebuilder,
        "get_tool_display",
        staticmethod(lambda _name: {}),
    )
    rows = [
        ConversationTaskContextRecord(
            task_id=2,
            run_id=3,
            sequence=1,
            include_in_context=True,
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "agent_team", "arguments": json.dumps({})},
                    }
                ],
            },
        ),
        ConversationTaskContextRecord(
            task_id=2,
            run_id=3,
            sequence=2,
            include_in_context=True,
            message={"role": "tool", "content": "失败", "tool_call_id": "call-1"},
            metadata=ToolMessageMetadata.model_validate(
                {
                    "status": "failed",
                    "error": "执行失败",
                    "display_data": {"status_hint": "执行失败"},
                    "team_run_id": 17,
                }
            ),
        ),
    ]

    part = ConversationTaskStateRebuilder.build_pair_tool_part(rows)["call-1"]
    assert part["team_run_id"] == 17
    assert part["display_data"] == {"status_hint": "执行失败"}


def test_team_view_marks_output_failure_on_completed_node(monkeypatch) -> None:
    """节点 Run 虽已完成，交接输出无效时流水线仍须明确指出该节点失败。"""

    state = AgentTeamRunState(
        current_node_id="review",
        node_runtime={
            "review": AgentTeamRunRuntime(
                agent_id="reviewer",
                node_goal_summary="审查",
            )
        },
    )
    row = SimpleNamespace(
        id=17,
        team_id="quality",
        workspace_id=1,
        parent_task_id=2,
        parent_run_id=3,
        goal_input="完成审查",
        status="failed",
        end_reason="node_output_invalid",
        started_at=None,
        ended_at=None,
        state_json=state.to_json(),
        configuration_snapshot_json={
            "start_node_id": "review",
            "nodes": [
                {
                    "node_id": "review",
                    "name": "审查",
                    "agent_id": "reviewer",
                    "statuses": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        run_service_module,
        "TaskCrud",
        lambda: SimpleNamespace(
            list_by_parent_task_ids=lambda _ids: {},
        ),
    )
    monkeypatch.setattr(
        run_service_module,
        "ConversationRunCrud",
        lambda: SimpleNamespace(
            get_many=lambda _ids: {},
        ),
    )
    monkeypatch.setattr(
        run_service_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda _id: SimpleNamespace(root_path="test"),
        ),
    )
    monkeypatch.setattr(
        run_service_module,
        "project_node_visits",
        lambda **_kwargs: {
            "review": [
                {
                    "visit_number": 1,
                    "conversation_run_id": 11,
                    "status": "completed",
                    "agents": [],
                }
            ],
        },
    )
    service = AgentTeamRunService.__new__(AgentTeamRunService)
    service.get = lambda _id: row

    view = service.get_view(17)

    assert view["nodes"][0]["status"] == "failed"
    assert view["nodes"][0]["visits"][0]["status"] == "completed"
    assert AgentTeamRunResponse.model_validate(view).nodes[0].status == "failed"


def test_invalid_node_output_is_not_an_unhandled_exception() -> None:
    """模型输出缺少状态时交给领域失败分支，不能因解包 None 丢失诊断。"""

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    assert coordinator.struct_out_put('{"status":"done","output":"ok"}') == ("done", "ok")
    assert coordinator.struct_out_put('{"output":"ok"}') is None
    assert coordinator.struct_out_put("not json") is None


def test_failed_background_team_result_carries_run_locator_and_archive_hint() -> None:
    """后台 Team 失败时，回投给主 Agent 的结果保留 TeamRun 定位、状态与档案读取指引。"""
    from app.service.agent_team.agent_team_decision_service import AgentTeamDecisionService

    row = SimpleNamespace(id=17, status="failed", end_reason="node_run_failed")
    service = AgentTeamDecisionService.__new__(AgentTeamDecisionService)
    service._archives = SimpleNamespace(
        existing_archive_path=lambda _id: ".cosir/agent-team-runs/17.json"
    )

    body = service._result_body(row)

    assert (body.job, body.job_id, body.status) == ("agent_team", 17, "failed")
    payload = json.loads(body.content)
    assert payload["end_reason"] == "node_run_failed"
    assert payload["trace_file"] == ".cosir/agent-team-runs/17.json"
    assert "read_file" in payload["read_instructions"]
    assert "hint" not in payload
