"""调度规则（``dispatch_policy``）逐条覆盖：纯函数，不依赖执行器与数据库。"""

from datetime import datetime

import pytest

from app.models import ConversationRunRecord
from app.models.conversation_input import ConversationInput
from app.models.conversation_run_options import ConversationRunOptions
from app.models.task_message import (
    AgentResultBody,
    BackgroundResultBody,
    DecisionBody,
    Delivery,
    NoticeBody,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.service.conversation_run import dispatch_policy
from app.service.conversation_run.dispatch_policy import (
    Deliver,
    LaunchLockedError,
    StartRun,
    Stay,
    Wake,
)

_LAUNCH = RunLaunch(model_config_id=1)
_OTHER = RunLaunch(model_config_id=2)


def _run(status: str, launch: RunLaunch = _LAUNCH) -> ConversationRunRecord:
    now = datetime.now()
    return ConversationRunRecord(
        id=10,
        task_id=1,
        status=status,
        created_at=now,
        updated_at=now,
        agent_id=launch.agent_id,
        model_config_id=launch.model_config_id,
        options=launch.options,
    )


def _user(
    message_id: int, launch: RunLaunch = _LAUNCH, delivery: Delivery = "immediate"
) -> TaskMessage:
    body = UserInputBody(
        message=ConversationInput(input_text="hi", display_text="hi"), launch=launch
    )
    return TaskMessage.of(body, delivery=delivery, message_id=message_id)


def _notice(message_id: int) -> TaskMessage:
    return TaskMessage.of(NoticeBody(content="n", notice_kind="k"), message_id=message_id)


def _decision(message_id: int) -> TaskMessage:
    body = DecisionBody(team_run_id=5, decision="approve")
    return TaskMessage.of(body, message_id=message_id)


def _agent_result(message_id: int) -> TaskMessage:
    body = AgentResultBody(child_task_id=8, status="completed", final_output="done")
    return TaskMessage.of(body, message_id=message_id)


def _result(message_id: int) -> TaskMessage:
    body = BackgroundResultBody(job="agent_team", job_id=5, status="completed", content="{}")
    return TaskMessage.of(body, message_id=message_id)


def test_executor_receives_only_immediate_messages() -> None:
    pending = [_user(1), _user(2, _OTHER, "next_run"), _notice(3)]
    action = dispatch_policy.next_action(_run("running"), True, pending, by_user=False)
    assert isinstance(action, Deliver)
    assert [m.id for m in action.messages] == [1, 3]


def test_user_send_after_stop_starts_run_with_latest_launch() -> None:
    pending = [_user(1), _notice(2), _user(3, _OTHER)]
    action = dispatch_policy.next_action(_run("cancelled"), False, pending, by_user=True)
    assert action == StartRun(_OTHER)


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_system_message_never_restarts_a_stopped_task(status: str) -> None:
    """用户挂起与失败只由用户恢复：残留输入与结果消息都不会自行开 Run。"""
    pending = [_user(1), _result(2)]
    assert dispatch_policy.next_action(_run(status), False, pending, by_user=False) == Stay()


@pytest.mark.parametrize("latest", [None, _run("completed"), _run("failed")])
def test_static_task_with_only_notices_stays(latest: ConversationRunRecord | None) -> None:
    assert dispatch_policy.next_action(latest, False, [_notice(1)], by_user=False) == Stay()


@pytest.mark.parametrize("message", [_user(1), _decision(1), _agent_result(1), _result(1)])
def test_suspended_run_wakes_on_any_wakeable_immediate_message(message: TaskMessage) -> None:
    action = dispatch_policy.next_action(_run("suspended"), False, [message], by_user=False)
    assert action == Wake()


def test_suspended_run_ignores_notices_and_next_run_inputs() -> None:
    pending = [_notice(1), _user(2, delivery="next_run")]
    assert dispatch_policy.next_action(_run("suspended"), False, pending, by_user=True) == Stay()


def test_completed_task_hands_off_queued_input_without_user_trigger() -> None:
    pending = [_user(1, _OTHER)]
    action = dispatch_policy.next_action(_run("completed"), False, pending, by_user=False)
    assert action == StartRun(_OTHER)


@pytest.mark.parametrize("message", [_agent_result(1), _result(1)])
def test_completed_task_wakes_on_results_with_inherited_launch(message: TaskMessage) -> None:
    action = dispatch_policy.next_action(_run("completed"), False, [message], by_user=False)
    assert action == StartRun(_LAUNCH)


def test_completed_task_ignores_decision_only() -> None:
    action = dispatch_policy.next_action(_run("completed"), False, [_decision(1)], by_user=False)
    assert action == Stay()


def test_results_are_dropped_after_user_cancel_but_kept_after_failure() -> None:
    body = _result(1).body
    assert dispatch_policy.admit(body, _run("cancelled")) is False
    assert dispatch_policy.admit(body, _run("failed")) is True
    assert dispatch_policy.admit(_agent_result(1).body, _run("cancelled")) is False
    assert dispatch_policy.admit(_decision(1).body, _run("cancelled")) is True


def test_fresh_run_receives_all_pending_resumed_run_only_immediate() -> None:
    pending = [_user(1), _user(2, delivery="next_run"), _notice(3)]
    assert dispatch_policy.initial_messages(pending, fresh_run=True) == pending
    resumed = dispatch_policy.initial_messages(pending, fresh_run=False)
    assert [m.id for m in resumed] == [1, 3]


def test_launch_is_not_locked_when_task_is_static() -> None:
    dispatch_policy.check_launch(_OTHER, "immediate", _run("completed"), False, [])


def test_immediate_input_must_match_active_run() -> None:
    dispatch_policy.check_launch(_LAUNCH, "immediate", _run("running"), True, [])
    with pytest.raises(LaunchLockedError):
        dispatch_policy.check_launch(_OTHER, "immediate", _run("running"), True, [])


def test_next_run_inputs_must_share_one_launch() -> None:
    dispatch_policy.check_launch(_OTHER, "next_run", _run("running"), True, [])
    queued = [_user(1, _OTHER, "next_run")]
    dispatch_policy.check_launch(_OTHER, "next_run", _run("running"), True, queued)
    with pytest.raises(LaunchLockedError):
        dispatch_policy.check_launch(_LAUNCH, "next_run", _run("running"), True, queued)


def test_ban_tools_compare_as_set() -> None:
    options = ConversationRunOptions(ban_tools=["b", "a"])
    run = _run("running", RunLaunch(model_config_id=1, options=options))
    launch = RunLaunch(model_config_id=1, options=ConversationRunOptions(ban_tools=["a", "b"]))
    assert dispatch_policy.matches_run(launch, run)
