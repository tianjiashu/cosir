"""失败的 Agent Team 仍可由工具调用定位，并能读取节点诊断。"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from app.agent_team.coordinator import AgentTeamCoordinator
from app.agent_team.state.agent_team_run_state import (
    AgentTeamNodeExecution,
    AgentTeamRunRuntime,
    AgentTeamRunState,
)
from app.api.schemas.response.agent_team_run_response import AgentTeamRunResponse
from app.assistant_transport.event.tool_call_event import ToolCallStatusChangedEvent
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_snapshot import validate_snapshot
from app.core.tools.schemas import UserDecision, UserDecisionKind
from app.core.tools.tool_handler.agent_team.agent_team_run import AgentTeamRunTool
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.service.agent_team import agent_team_execution_timeline
from app.service.agent_team import agent_team_run_service as run_service_module
from app.service.agent_team.agent_team_execution_timeline import project_node_visits
from app.service.agent_team.agent_team_run_service import AgentTeamRunService


def test_failed_tool_event_keeps_team_run_locator_outside_display_data() -> None:
    """错误提示覆盖展示数据时，持久化运行引用仍须留在独立字段。"""

    state = {
        "runs": [{
            "runId": 3, "status": "running", "endReason": None,
            "langfuseTraceId": None, "messages": [{
                "id": "message-1", "role": "assistant", "parts": [{
                    "type": "tool-call", "toolCallId": "call-1", "toolName": "agent_team",
                    "status": "running", "presentation": {},
                    "display_data": {"kind": "agent-team-approval", "team_run_id": 17},
                }],
            }], "usage": None, "error": None,
        }],
        "current_run_id": 3, "approvals": {}, "context_window_total": None, "error": None,
    }
    event = ToolCallStatusChangedEvent(
        task_id=2, run_id=3, tool_call_id="call-1", status="failed",
        error="执行失败", display_data={"status_hint": "执行失败"}, team_run_id=17,
    )

    for mutation in event.plan(state):
        target = state
        for key in mutation.path[:-1]:
            target = target[key]
        target[mutation.path[-1]] = mutation.value

    part = state["runs"][0]["messages"][0]["parts"][0]
    assert part["status"] == "failed"
    assert part["display_data"] == {"status_hint": "执行失败"}
    assert part["team_run_id"] == 17
    validate_snapshot(state)


def test_failed_node_visit_exposes_run_error_without_runtime_snapshot(monkeypatch) -> None:
    """节点错误只取 Run 的用户可读错误契约，不读取 Team 的执行资源快照。"""

    monkeypatch.setattr(
        agent_team_execution_timeline, "get_agent_profile_registry",
        lambda: SimpleNamespace(resolve=lambda *_args: SimpleNamespace(role="开发")),
    )
    state = AgentTeamRunState(
        node_executions={"implement": AgentTeamNodeExecution(
            node_id="implement", task_id=8, conversation_run_ids=[11],
        )},
        current_node_id="implement",
        node_runtime={"implement": AgentTeamRunRuntime(
            agent_id="developer", node_goal_summary="实现",
        )},
    )
    run = SimpleNamespace(
        status="failed", end_reason="model_resolve_failed", final_output=None,
        agent_id="developer", error={
            "code": "model_resolve_failed", "message": "模型配置不可用，请检查模型设置",
        },
    )

    visits = project_node_visits(
        state=state,
        configuration={"nodes": [{
            "node_id": "implement", "name": "实现", "agent_id": "developer", "statuses": [],
        }]},
        team_status="failed", run_records={11: run}, child_tasks_by_parent={}, workspace="test",
    )

    assert visits["implement"][0]["failure_message"] == "模型配置不可用，请检查模型设置"
    assert "node_runtime" not in visits["implement"][0]


def test_cold_rebuild_keeps_failed_team_run_locator(monkeypatch) -> None:
    """刷新页面后从 ToolMessage 元数据恢复定位信息，不依赖进程内快照。"""

    monkeypatch.setattr(
        ConversationTaskStateRebuilder, "get_tool_display", staticmethod(lambda _name: {}),
    )
    rows = [
        ConversationTaskContextRecord(
            task_id=2, run_id=3, sequence=1, include_in_context=True,
            message=AIMessage(content="", tool_calls=[{
                "name": "agent_team", "args": {}, "id": "call-1",
            }]),
        ),
        ConversationTaskContextRecord(
            task_id=2, run_id=3, sequence=2, include_in_context=True,
            message=ToolMessage(content="失败", tool_call_id="call-1"),
            transport_metadata={
                "status": "failed", "error": "执行失败",
                "display_data": {"status_hint": "执行失败"}, "team_run_id": 17,
            },
        ),
    ]

    part = ConversationTaskStateRebuilder.build_pair_tool_part(rows)["call-1"]
    assert part["team_run_id"] == 17
    assert part["display_data"] == {"status_hint": "执行失败"}


def test_team_view_marks_output_failure_on_completed_node(monkeypatch) -> None:
    """节点 Run 虽已完成，交接输出无效时流水线仍须明确指出该节点失败。"""

    state = AgentTeamRunState(
        current_node_id="review",
        node_runtime={"review": AgentTeamRunRuntime(
            agent_id="reviewer", node_goal_summary="审查",
        )},
    )
    row = SimpleNamespace(
        id=17, team_id="quality", workspace_id=1, parent_task_id=2, parent_run_id=3,
        goal_input="完成审查", status="failed", end_reason="node_output_invalid",
        started_at=None, ended_at=None, state_json=state.to_json(),
        configuration_snapshot_json={
            "start_node_id": "review", "nodes": [{
                "node_id": "review", "name": "审查", "agent_id": "reviewer", "statuses": [],
            }],
        },
    )
    monkeypatch.setattr(run_service_module, "TaskCrud", lambda: SimpleNamespace(
        list_by_parent_task_ids=lambda _ids: {},
    ))
    monkeypatch.setattr(run_service_module, "ConversationRunCrud", lambda: SimpleNamespace(
        get_many=lambda _ids: {},
    ))
    monkeypatch.setattr(run_service_module, "get_workspace_service", lambda: SimpleNamespace(
        get_workspace=lambda _id: SimpleNamespace(root_path="test"),
    ))
    monkeypatch.setattr(run_service_module, "project_node_visits", lambda **_kwargs: {
        "review": [{
            "visit_number": 1, "conversation_run_id": 11, "status": "completed",
            "business_status": None, "business_status_color": None,
            "failure_message": None, "agents": [],
        }],
    })
    service = AgentTeamRunService.__new__(AgentTeamRunService)
    service.get = lambda _id: row

    view = service.get_view(17)

    assert view["failure_node_id"] == "review"
    assert view["nodes"][0]["status"] == "failed"
    assert view["nodes"][0]["visits"][0]["status"] == "completed"
    assert AgentTeamRunResponse.model_validate(view).failure_node_id == "review"


def test_invalid_node_output_is_not_an_unhandled_exception() -> None:
    """模型输出缺少状态时交给领域失败分支，不能因解包 None 丢失诊断。"""

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    assert coordinator.struct_out_put('{"status":"done","output":"ok"}') == ("done", "ok")
    assert coordinator.struct_out_put('{"output":"ok"}') is None
    assert coordinator.struct_out_put("not json") is None


def test_confirmed_failed_team_returns_error_with_run_locator() -> None:
    """同步启动后立即失败时保留工具失败语义，同时让 UI 找回整个 TeamRun。"""

    row = SimpleNamespace(
        id=17, team_id="quality", status="failed", end_reason="node_run_failed",
    )
    tool = AgentTeamRunTool.__new__(AgentTeamRunTool)
    tool.agent_team_run_service = SimpleNamespace(
        confirm_and_start=lambda *_args, **_kwargs: row,
    )
    decision = UserDecision(
        request_id="17", kind=UserDecisionKind.APPROVE,
        data={
            "configuration": {}, "goal": "完成审查",
            "node_goals": {"review": {"goal": "检查代码", "summary": "审查代码"}},
        },
    )

    observation = tool._start_confirmed_run(decision, SimpleNamespace())

    assert observation.status == "error"
    assert observation.team_run_id == 17
    assert observation.display_data == {"status_hint": "执行失败"}
