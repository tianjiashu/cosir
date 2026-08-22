"""``ChildAgentRunner`` 委派 child 运行结果压缩的单元测试。

验证「max_steps 失败默认文本可达」修复中父 Agent 侧的收敛：child 因步数耗尽而失败时，
``RUN_FAILED`` 事件携带 ``end_reason="max_steps_reached"``，``ChildAgentRunner`` 应把该
枚举码翻译为父 Agent 可读的中文说明（而非把 "max_steps_reached" 原样塞进结果）；
其它普通失败仍走原 ``error``/``message`` 回退。
"""

from collections.abc import AsyncGenerator

from app.core.delegation.child_agent_runner import ChildAgentRunner
from app.models.enums.event_type import EventType
from app.models.event.runtime_event import RuntimeEvent
from app.models.payload import RunFailedPayload
from app.models.result.delegation_result import DelegationResult


def _make_profile(turn_id: str):
    """构造仅暴露 ``turn.turn_id`` 的 child profile 桩。

    参数:
        turn_id: 期望的 child turn id。
    返回:
        具备 ``turn.turn_id`` 属性的轻量桩对象（测试只读该字段）。
    """
    return type("ProfileStub", (), {"turn": type("TurnStub", (), {"turn_id": turn_id})()})()


async def _events(*events: RuntimeEvent) -> AsyncGenerator[RuntimeEvent, None]:
    """把给定事件包装为异步生成器。

    参数:
        events: 依次 yield 的 ``RuntimeEvent``。
    返回:
        逐个产出事件的异步生成器。
    """
    for event in events:
        yield event


def test_run_child_max_steps_reached_uses_readable_summary() -> None:
    """child 因步数耗尽失败时，父 Agent 拿到中文说明而非 ``max_steps_reached`` 枚举码。"""
    failed = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id="task-1",
        turn_id="child-turn-1",
        payload=RunFailedPayload(
            error="max_steps_reached",
            end_reason="max_steps_reached",
            step_id="step-1",
        ),
    )

    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        yield failed

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-1"))

    assert result.status == "failed"
    assert result.error is not None
    assert "最大步骤数" in result.error
    assert "max_steps_reached" not in result.error


def test_run_child_plain_failure_keeps_error() -> None:
    """普通 child 失败（无 end_reason 枚举码）仍保留原始 error 文本。"""
    failed = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id="task-1",
        turn_id="child-turn-2",
        payload=RunFailedPayload(error="boom"),
    )

    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        yield failed

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-2"))

    assert result.status == "failed"
    assert result.error == "boom"


def test_run_child_final_response_summary() -> None:
    """child 正常完成时，父 Agent 结果 summary 取 ``FINAL_RESPONSE.text``。"""
    from app.models.payload import FinalResponsePayload, RunFinishedPayload

    final = RuntimeEvent(
        event_type=EventType.FINAL_RESPONSE,
        task_id="task-1",
        turn_id="child-turn-3",
        payload=FinalResponsePayload(text="done", step_id="step-1", status="completed"),
    )
    finished = RuntimeEvent(
        event_type=EventType.RUN_FINISHED,
        task_id="task-1",
        turn_id="child-turn-3",
        payload=RunFinishedPayload(status="completed"),
    )

    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        yield final
        yield finished

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-3"))

    assert result.status == "completed"
    assert result.summary == "done"


def test_run_child_failure_falls_back_to_message_when_error_missing() -> None:
    """普通失败时 error 缺失但 message 存在，应回退到 message（防御回退链的 message 分支）。"""
    failed = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id="task-1",
        turn_id="child-turn-4",
        payload=RunFailedPayload(error="", message="child exploded"),
    )

    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        yield failed

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-4"))

    assert result.status == "failed"
    assert result.error == "child exploded"


def test_run_child_failure_other_end_reason_keeps_raw_error() -> None:
    """end_reason 为其它语义化枚举值（非 max_steps_reached）时，仍保留原始 error 不混淆。"""
    failed = RuntimeEvent(
        event_type=EventType.RUN_FAILED,
        task_id="task-1",
        turn_id="child-turn-5",
        payload=RunFailedPayload(
            error="client_disconnected",
            end_reason="client_disconnected",
        ),
    )

    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        yield failed

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-5"))

    assert result.status == "failed"
    # 非 max_steps_reached 的枚举码不应被翻译为 max_steps 中文说明
    assert result.error == "client_disconnected"
    assert "最大步骤数" not in result.error


def test_run_child_no_terminal_event_fails() -> None:
    """事件流无终态事件（既无 RUN_FINISHED 也无 RUN_FAILED）时返回 failed。"""
    async def run_agent(_profile) -> AsyncGenerator[RuntimeEvent, None]:
        if False:
            yield None

    runner = ChildAgentRunner(run_agent)
    result: DelegationResult = runner.run_child(_make_profile("child-turn-6"))

    assert result.status == "failed"
    assert result.error == "child turn ended without terminal event"
