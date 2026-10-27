"""调度规则（``dispatch_policy``）逐条覆盖：纯函数，不依赖执行器与数据库。

规则：有消息就处理（系统通知例外）。只看新到达的消息决定是否推进，残留消息随推进一并投递。
"""

from datetime import datetime

import pytest

from app.core.workflow.inbox import dispatch_policy
from app.core.workflow.inbox.dispatch_policy import Deliver, StartRun, Stay, Wake
from app.core.workflow.inbox.task_message import (
    BackgroundResultBody,
    DecisionBody,
    Delivery,
    NoticeBody,
    RunLaunch,
    TaskMessage,
    UserInputBody,
)
from app.models import ConversationRunRecord
from app.models.conversation_run_options import ConversationRunOptions

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
    body = UserInputBody(display_text="hi", input_text="hi", launch=launch)
    return TaskMessage.of(body, delivery=delivery, message_id=message_id)


def _notice(message_id: int) -> TaskMessage:
    return TaskMessage.of(NoticeBody(content="n", notice_kind="k"), message_id=message_id)


def _decision(message_id: int) -> TaskMessage:
    body = DecisionBody(tool_call_id="c1", tool_name="hitl_tool", decision="approve")
    return TaskMessage.of(body, message_id=message_id)


def _result(message_id: int) -> TaskMessage:
    return TaskMessage.of(
        BackgroundResultBody(tool_name="probe", tool_result="done"), message_id=message_id
    )


def _next(current, driving, pending, fresh):
    phase = dispatch_policy.phase_of(current, driving)
    return dispatch_policy.next_action(phase, current, pending, fresh)


@pytest.mark.parametrize(
    ("status", "driving", "phase"),
    [
        # 执行者优先：Run 已落定、执行者还在收尾时仍是 driving。
        ("running", True, "driving"),
        ("completed", True, "driving"),
        ("suspended", True, "driving"),
        ("suspended", False, "suspended"),
        ("completed", False, "idle"),
        ("cancelled", False, "idle"),
        ("failed", False, "idle"),
        (None, False, "idle"),
    ],
)
def test_phase_prefers_executor_over_run_status(status, driving, phase) -> None:
    current = _run(status) if status is not None else None
    assert dispatch_policy.phase_of(current, driving) == phase


@pytest.mark.parametrize("status", ["completed", "suspended"])
def test_executor_still_settling_receives_deliveries_not_a_new_run(status) -> None:
    """Run 已落定但执行者未释放：只投递，不新开、不唤醒——否则同一 Task 出现两个执行者。"""
    message = _user(1)
    assert _next(_run(status), True, [message], [message]) == Deliver([message])


def test_executor_receives_only_immediate_messages() -> None:
    pending = [_user(1), _user(2, _OTHER, "next_run"), _notice(3)]
    action = _next(_run("running"), True, pending, pending)
    assert action == Deliver([pending[0], pending[2]])


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
@pytest.mark.parametrize("make", [_user, _decision, _result])
def test_any_new_message_starts_a_run_after_any_terminal_status(status, make) -> None:
    """有消息就处理：取消、失败之后到达的后台结果与决定同样开出新 Run。"""
    message = make(1)
    action = _next(_run(status), False, [message], [message])
    assert isinstance(action, StartRun)


def test_new_run_takes_the_latest_user_input_launch() -> None:
    pending = [_user(1, _OTHER, "next_run"), _user(2, _LAUNCH, "next_run")]
    action = _next(_run("completed", _OTHER), False, pending, pending[-1:])
    assert action == StartRun(_LAUNCH)


def test_new_run_without_user_input_inherits_the_current_run_launch() -> None:
    pending = [_result(1)]
    action = _next(_run("cancelled", _OTHER), False, pending, pending)
    assert action == StartRun(_OTHER)


def test_no_launch_to_inherit_stays() -> None:
    action = _next(None, False, [_result(1)], [_result(1)])
    assert action == Stay()


@pytest.mark.parametrize("latest", [None, _run("completed"), _run("cancelled")])
def test_notices_alone_never_start_a_run(latest: ConversationRunRecord | None) -> None:
    action = _next(latest, False, [_notice(1)], [_notice(1)])
    assert action == Stay()


def test_only_new_messages_trigger_leftovers_do_not() -> None:
    """残留消息（已判定过、尚未消费）不会再次触发推进。"""
    leftover = _user(1)
    action = _next(_run("cancelled"), False, [leftover], [])
    assert action == Stay()


@pytest.mark.parametrize("make", [_user, _decision, _result])
def test_suspended_run_wakes_on_new_wakeable_immediate_message(make) -> None:
    message = make(1)
    assert _next(_run("suspended"), False, [message], [message]) == Wake()


def test_suspended_run_ignores_notices_and_next_run_inputs() -> None:
    pending = [_notice(1), _user(2, delivery="next_run")]
    assert _next(_run("suspended"), False, pending, pending) == Stay()


def test_fresh_run_receives_all_pending_resumed_run_only_immediate() -> None:
    pending = [_user(1, delivery="next_run"), _notice(2)]
    assert dispatch_policy.initial_messages(pending, fresh_run=True) == pending
    assert dispatch_policy.initial_messages(pending, fresh_run=False) == [pending[1]]


def test_ban_tools_compare_as_set() -> None:
    options = ConversationRunOptions(ban_tools=["b", "a"])
    run = _run("running", RunLaunch(model_config_id=1, options=options))
    launch = RunLaunch(model_config_id=1, options=ConversationRunOptions(ban_tools=["a", "b"]))
    assert dispatch_policy.matches_run(launch, run)
