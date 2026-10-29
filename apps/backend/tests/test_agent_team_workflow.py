"""Agent Team 预览展示契约与结果投影测试。"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.core.tool.handler.agent_team.coordinator as coordinator_module
from app.core.tool.handler.agent_team.coordinator import AgentTeamCoordinator
from app.core.tool.handler.agent_team.team_tool_error import TeamToolError
from app.api.schemas.response.agent_team_run_response import AgentTeamRunResponse
from app.core.agents.model_settings import ModelSettings
from app.core.tool.handler.agent_team.agent_team_display import (
    build_agent_team_preview_display_data,
)
from app.models.enums.agent_team_run_end_reason import AgentTeamRunEndReason
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.core.tool.handler.agent_team.agent_team_preparation_service import (
    resolve_node_model,
)


def _preview_fields() -> dict[str, object]:
    """构造准备服务实际会给出的预览字段（含用户可编辑的目标与子目标）。"""

    return {
        "team_id": "code-quality",
        "name": "代码质量 Team",
        "goal": "交付报告",
        "node_goals": {
            "develop": {"goal": "实现", "summary": "实现"},
            "review": {"goal": "审查", "summary": "审查"},
        },
        "start_node": "develop",
        "nodes": [],
        "edges": [],
        "parent_task_id": 2,
        "parent_run_id": 3,
        "workspace_id": 1,
        "configuration": {"team_id": "code-quality"},
    }


def test_agent_team_preview_carries_only_card_content() -> None:
    """展示数据只承载卡片内容与 TeamRun 定位；「待确认」由 TeamRun 的 pending 状态表达。"""

    display_data = build_agent_team_preview_display_data(_preview_fields(), team_run_id=17)

    assert display_data["kind"] == "agent-team-approval"
    assert display_data["team_run_id"] == 17
    assert "requires_user_input" not in display_data
    assert "user_input_request" not in display_data


def test_cancelling_team_run_only_transitions_status_and_writes_archive(monkeypatch) -> None:
    """取消入口只做条件迁移与档案，不碰节点：节点由 ``start`` 的等待循环唯一负责停下。"""

    updated = SimpleNamespace(
        id=17,
        parent_run_id=23,
        status=AgentTeamRunStatus.CANCELLED.value,
        state_json={},
    )
    captured: dict[str, object] = {}

    class _Crud:
        def update_status_if_in(
            self,
            run_id: int,
            target: str,
            allowed: tuple[str, ...],
            **kwargs: object,
        ) -> SimpleNamespace:
            captured.update(run_id=run_id, target=target, allowed=allowed, **kwargs)
            return updated

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _Crud()
    coordinator._run_archive_service = SimpleNamespace(
        write_terminal_archive=lambda run_id: captured.update(archived=run_id)
    )
    monkeypatch.setattr(
        coordinator_module.task_runtime_spaces,
        "get_or_create",
        lambda task_id: SimpleNamespace(actor=SimpleNamespace(
            request_cancel=lambda reason: captured.update(signalled=(task_id, reason))
        )),
    )

    result = coordinator.cancel(17)

    assert result is updated
    assert captured["allowed"] == (
        AgentTeamRunStatus.RUNNING.value,
        AgentTeamRunStatus.PENDING.value,
    )
    assert captured["target"] == AgentTeamRunStatus.CANCELLED.value
    assert captured["end_reason"] == AgentTeamRunEndReason.CANCELLED.value
    assert captured["ended"] is True
    assert "signalled" not in captured
    assert captured["archived"] == 17


def test_cancel_returns_current_row_when_team_run_already_settled() -> None:
    """条件迁移落空（并发落定或已在终态）时返回当前快照而不是 None，API 才能照常投影。"""

    settled = SimpleNamespace(id=17, status=AgentTeamRunStatus.COMPLETED.value)
    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = SimpleNamespace(
        update_status_if_in=lambda *_args, **_kwargs: None,
        get_by_id=lambda _run_id: settled,
    )

    assert coordinator.cancel(17) is settled


def test_parent_task_stop_cancels_pending_and_running_teams_of_that_task() -> None:
    """主 Task 停止按 Task 寻址：待确认方案与在新 Run 里执行的 Team 都取消，别的 Task 不受影响。"""

    pending = SimpleNamespace(id=1, parent_task_id=5, parent_run_id=10)
    # 方案在 Run 10 提出、批准后由 Run 11 执行：按执行中的 Run 匹配会漏掉它。
    running = SimpleNamespace(id=2, parent_task_id=5, parent_run_id=10)
    other = SimpleNamespace(id=3, parent_task_id=6, parent_run_id=12)
    transitions: list[tuple[int, str]] = []

    class _Crud:
        def list_pending_confirmations(self) -> list[SimpleNamespace]:
            return [pending]

        def list_active(self) -> list[SimpleNamespace]:
            return [running, other]

        def update_status_if_in(self, run_id: int, _target: str, _allowed, **kwargs: object):
            transitions.append((run_id, str(kwargs["end_reason"])))
            return SimpleNamespace(id=run_id, parent_run_id=10)

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _Crud()
    coordinator._run_archive_service = SimpleNamespace(write_terminal_archive=lambda _run_id: None)

    assert coordinator.cancel_for_parent_task(5) == 2
    assert transitions == [
        (1, AgentTeamRunEndReason.PARENT_CANCELLED.value),
        (2, AgentTeamRunEndReason.PARENT_CANCELLED.value),
    ]


def _driven_coordinator(monkeypatch, run_node) -> tuple[AgentTeamCoordinator, dict[str, object]]:
    """构造只替换存储与节点执行的协调器，驱动真实的 ``start`` 调度循环。"""

    captured: dict[str, object] = {"updates": [], "cancelled": []}

    class _Crud:
        def update_status_if_in(self, run_id, target, allowed, **kwargs):
            captured["updates"].append((target, kwargs))
            return SimpleNamespace(id=run_id)

        def get_by_id(self, run_id):
            return SimpleNamespace(
                id=run_id,
                status=AgentTeamRunStatus.RUNNING.value,
                parent_task_id=1,
                tool_call_id="call_team",
            )

    coordinator = AgentTeamCoordinator.__new__(AgentTeamCoordinator)
    coordinator._team_run_crud = _Crud()
    coordinator._run_archive_service = SimpleNamespace(write_terminal_archive=lambda _id: None)
    coordinator._run_node = run_node
    monkeypatch.setattr(
        coordinator_module,
        "AgentTeamConfiguration",
        SimpleNamespace(
            model_validate=lambda _value: SimpleNamespace(start_node_id="develop", max_runs=3)
        ),
    )
    monkeypatch.setattr(
        coordinator_module.task_runtime_spaces,
        "get_or_create",
        lambda task_id: SimpleNamespace(actor=SimpleNamespace(
            request_cancel=lambda reason: captured["cancelled"].append((task_id, reason))
        )),
    )
    return coordinator, captured


_TEAM_RUN = SimpleNamespace(
    id=17,
    state_json={"node_transitions": {"develop": {"done": "END"}}},
    configuration_snapshot_json={},
)


def test_completed_team_does_not_store_node_output_as_end_reason(monkeypatch) -> None:
    """``end_reason`` 只承载原因码：Team 完成时不得把节点交接输出写进去。"""

    def run_node(_row, _state, *_args):
        return SimpleNamespace(
            id=5,
            status="completed",
            final_output='{"status": "done", "output": "很长的最终报告"}',
        )

    coordinator, captured = _driven_coordinator(monkeypatch, run_node)
    coordinator.start(_TEAM_RUN, event_loop=None)

    target, kwargs = captured["updates"][-1]
    assert target == AgentTeamRunStatus.COMPLETED.value
    assert kwargs.get("end_reason") is None


def test_dispatch_failure_after_enqueue_stops_the_node_task(monkeypatch) -> None:
    """输入已投递后调度出错（如等新 Run 超时）：TeamRun 收敛为 failed，并停下节点 Task。"""

    from app.models.agent_team.state.agent_team_run_state import AgentTeamNodeExecution

    def run_node(_row, state, *_args):
        state.create_execution_node(
            "develop", AgentTeamNodeExecution(node_id="develop", task_id=41)
        )
        raise RuntimeError("节点输入没有启动新 Run")

    coordinator, captured = _driven_coordinator(monkeypatch, run_node)
    coordinator.start(_TEAM_RUN, event_loop=None)

    target, kwargs = captured["updates"][-1]
    assert target == AgentTeamRunStatus.FAILED.value
    assert kwargs["end_reason"] == AgentTeamRunEndReason.NEXT_NODE_START_FAILED.value
    assert captured["cancelled"] == [(41, "parent")]


def test_team_run_view_is_typed_and_does_not_accept_runtime_state() -> None:
    """前端 TeamRun 契约只允许安全投影字段，不携带恢复执行快照。"""

    payload = {
        "id": 1,
        "team_id": "quality",
        "goal": "goal",
        "status": "running",
        "nodes": [{
            "node_id": "review", "name": "Review", "agent_id": "reviewer",
            "role": "审查", "goal_summary": "检查改动", "status": "running",
            "visits": [{
                "visit_number": 1, "conversation_run_id": 10, "status": "completed",
                "agents": [{
                    "task_id": 9, "title": "Review", "agent_id": "reviewer",
                    "role": "审查", "status": "completed",
                }],
            }],
        }],
        "current_node_id": "review",
    }
    view = AgentTeamRunResponse.model_validate(payload)
    assert view.nodes[0].visits[0].agents[0].task_id == 9
    assert "state" not in view.model_dump()
    # 恢复执行用的内部 state 不得经响应契约下发。
    with pytest.raises(ValidationError):
        AgentTeamRunResponse.model_validate({**payload, "state": {"node_runtime": {}}})


def test_team_node_model_falls_back_to_parent_model_config_and_keeps_own_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有独立模型配置的节点：沿用主 Run 选用的模型配置，节点偏好覆盖、主 Agent 偏好补缺。"""
    from app.core.agents.resolved_model import ResolvedModel
    from app.models.model_config_record import ModelConfigRecord
    from app.core.tool.handler.agent_team import agent_team_preparation_service as preparation

    config = ModelConfigRecord(
        config_name="local",
        base_url="http://127.0.0.1/v1",
        api_key="secret",
        model_name="local-model",
        context_window_k=32,
        supports_thinking=True,
        supports_reasoning_effort=True,
        id=9,
    )
    monkeypatch.setattr(
        preparation,
        "get_model_config_service",
        lambda: SimpleNamespace(get_config=lambda config_id: config),
    )
    child_profile = SimpleNamespace(
        agent_id="general-assistant",
        model_config_id=None,
        model_settings=ModelSettings(reasoning_effort="high"),
    )
    parent_agent = SimpleNamespace(
        model=ResolvedModel(
            model_config_id=9,
            base_url=config.base_url,
            api_key=config.api_key,
            model_name=config.model_name,
            context_window_k=32,
            supports_thinking=True,
            supports_reasoning_effort=True,
            supports_image=False,
            temperature=0.3,
            reasoning_effort="low",
        )
    )

    node_model = resolve_node_model(child_profile, parent_agent)

    assert node_model.model_config_id == 9
    assert node_model.model_name == "local-model"
    assert node_model.reasoning_effort == "high"
    assert node_model.temperature == 0.3


def test_team_node_without_any_model_config_is_rejected() -> None:
    child_profile = SimpleNamespace(
        agent_id="general-assistant",
        model_config_id=None,
        model_settings=ModelSettings.default_settings(),
    )

    with pytest.raises(TeamToolError):
        resolve_node_model(child_profile, None)

