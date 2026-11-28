# ruff: noqa: F811 - ``derived`` 夹具从 test_derived_report 导入后作参数
"""上游收回的边界：截止之外开出的 Run、三层派生里孙会话的迟到汇报、刚物化的 space、兄弟互不连累。

真实 actor、ReAct、delegate_task、撤回服务、派生汇报与隔离 SQLite；模型是按输入路由的确定性替身。
时序用屏障固定在真实路径会发生的等待点上（开 Run 的写库、收回下级的工作线程），不靠 sleep 赌时序。
"""

import asyncio
import json
import threading
from types import SimpleNamespace

from app.core import session_recall as session_recall_module
from app.core.session_actor import SessionActor
from app.core.session_recall import SessionRecall
from app.core.workflow.message_queue.session_message import (
    AgentMessageBody,
    RunLaunch,
    SessionMessage,
)
from app.core.workflow.run_ledger import RunLedger
from app.models.message_context.conversation_input import MessageSource
from app.service import depends
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces as spaces
from tests.sdk_fixtures import sdk_events
from tests.test_derived_report import (  # noqa: F401 - derived 是夹具
    ASSIGNMENT,
    _child_id,
    _Model,
    _pending_kinds,
    _runs,
    _send,
    _settle,
    _until,
    derived,
)

GRAND_ASSIGNMENT = "grand-work"


def _delegate(call_id: str, agent_name: str, message: str) -> dict:
    return {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {
            "name": "delegate_task",
            "arguments": json.dumps(
                {"child_agent_id": "general-purpose", "agent_name": agent_name, "message": message}
            ),
        },
    }


class _ThreeLevelModel:
    """P 委派 C，C 委派 G；G 卡在 ``grand_gate`` 上。C 先答中间结果，收到 G 的汇报后答 ``child done``。"""

    def __init__(self) -> None:
        self.grand_gate = asyncio.Event()
        self.child_gate = self.grand_gate  # 夹具收尾时放行
        self.grand_started = asyncio.Event()
        self.parent_reports: list[str] = []

    async def stream(self, messages, *, tools):
        inputs = [str(m.get("content")) for m in messages if m["role"] == "user"]
        tools_done = [m for m in messages if m["role"] == "tool"]
        if any(GRAND_ASSIGNMENT in text for text in inputs):
            self.grand_started.set()
            await self.grand_gate.wait()
            frames = sdk_events([{"content": "grand result"}])
        elif any(ASSIGNMENT in text and 'from="parent"' in text for text in inputs):
            if not tools_done:
                frames = sdk_events(
                    [{"tool_calls": [_delegate("call-grand", "grand", GRAND_ASSIGNMENT)]}],
                    finish_reason="tool_calls",
                )
            else:
                done = any("grand result" in text for text in inputs)
                frames = sdk_events([{"content": "child done" if done else "child result"}])
        else:
            reports = [text for text in inputs if "report=" in text]
            if not tools_done and not reports:
                frames = sdk_events(
                    [{"tool_calls": [_delegate("call-delegate", "review", ASSIGNMENT)]}],
                    finish_reason="tool_calls",
                )
            else:
                self.parent_reports = reports
                frames = sdk_events([{"content": f"parent saw {len(reports)} reports"}])
        for frame in frames:
            yield frame

    async def close(self):
        pass


async def _three_levels(derived) -> tuple[int, int]:
    """跑到：P 两个 Run 都完成（第二个读到 C 的中间汇报）、C 空闲、G 卡在模型里；返回 (C, G)。"""
    derived.model = _ThreeLevelModel()
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.grand_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    grand_id = _child_id(child_id)
    parent_actor = spaces.get_or_create(derived.parent.id).actor
    child_actor = spaces.get_or_create(child_id).actor
    await _until(
        derived.parent.id,
        lambda: len(_runs(derived.parent.id)) == 2
        and _runs(derived.parent.id)[-1].status == "completed"
        and not parent_actor.driving
        and not child_actor.driving,
    )
    return child_id, grand_id


async def test_control_three_levels_report_up_normally(derived):
    """对照：不收回，G 的结论经 C 再交给 P。"""
    child_id, grand_id = await _three_levels(derived)
    derived.model.grand_gate.set()
    await _until(
        derived.parent.id,
        lambda: _runs(derived.parent.id)[-1].final_output == "parent saw 2 reports",
    )
    await _settle(grand_id, child_id, derived.parent.id)
    assert [run.final_output for run in _runs(child_id)] == ["child result", "child done"]
    assert _runs(derived.parent.id)[-1].final_output == "parent saw 2 reports"


async def test_grandchild_report_landing_during_the_recall_does_not_reach_the_top(derived, monkeypatch):
    """B1：P 收回 C（P 的 Agent 调 ``session_stop``，与撤回、逐层收回同一入口）；C 收回 G 之前，G 的汇报
    已进了 C 的队列。C 不能据此开出 Run 再向 P 汇报。

    屏障放在 C 收回下级的工作线程上：收回在那里读派生关系、写截止，真实路径上本就要等写库。
    """
    child_id, grand_id = await _three_levels(derived)
    recall_reached, recall_release = threading.Event(), threading.Event()
    original = SessionRecall._recall_derived

    def gated(self):
        if self.session_id == child_id:
            recall_reached.set()
            assert recall_release.wait(10), "recall was never released"
        return original(self)

    monkeypatch.setattr(SessionRecall, "_recall_derived", gated)
    try:
        await asyncio.to_thread(spaces.get_or_create(child_id).actor.request_recall)
        assert await asyncio.to_thread(recall_reached.wait, 8)
        derived.model.grand_gate.set()
        await _until(grand_id, lambda: _runs(grand_id)[-1].status == "completed")
        await _until(grand_id, lambda: not spaces.get_or_create(grand_id).actor.driving)
    finally:
        recall_release.set()
    await _settle(grand_id, child_id, derived.parent.id)

    assert len(_runs(derived.parent.id)) == 2, [run.final_output for run in _runs(derived.parent.id)]
    assert _pending_kinds(derived.parent.id) == []


async def test_a_run_opened_after_the_cutoff_and_then_recalled_leaves_no_notice(derived, monkeypatch):
    """B2：C 正在开新 Run（已持闸门、还没提交）时被收回：新 Run 在截止之后，随即以 parent 停下。
    派生方不要它的任何东西，连「被停止」的通知也不该有。"""
    derived.model = _Model("block")
    derived.model.child_gate.set()
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await _settle(child_id, derived.parent.id)
    parent_runs = len(_runs(derived.parent.id))

    creating, release = threading.Event(), threading.Event()
    original = RunLedger.create_run

    def gated(self, session_id, launch):
        if session_id == child_id:
            creating.set()
            assert release.wait(10), "create_run was never released"
        return original(self, session_id, launch)

    monkeypatch.setattr(RunLedger, "create_run", gated)
    derived.model.child_gate.clear()
    spaces.enqueue(
        child_id,
        SessionMessage.of(
            AgentMessageBody(
                text=f"{ASSIGNMENT} more",
                source=MessageSource(session_id=derived.parent.id, name="parent"),
                launch=RunLaunch(model_config_id=derived.model_config.id),
            )
        ),
    )
    try:
        assert await asyncio.to_thread(creating.wait, 8)
        await asyncio.to_thread(spaces.get_or_create(child_id).actor.request_recall)
    finally:
        release.set()
    await _until(child_id, lambda: len(_runs(child_id)) == 2 and _runs(child_id)[-1].status != "running")
    derived.model.child_gate.set()
    await _settle(child_id, derived.parent.id)

    assert _runs(child_id)[-1].end_reason == "parent_cancelled"
    assert len(_runs(derived.parent.id)) == parent_runs
    assert _pending_kinds(derived.parent.id) == []


async def test_recall_on_a_freshly_materialized_space_is_not_dropped(derived, monkeypatch):
    """B3：重启后没物化的会话被收回：工作线程里 ``get_or_create`` 之后立刻收回，停止不能丢。"""
    derived.model = _Model("answer")
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(derived.model.child_started.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await _settle(child_id, derived.parent.id)
    await spaces.aclose()
    spaces.bind_loop(asyncio.get_running_loop())
    assert spaces.get(child_id) is None

    cancels: list[tuple[int, str]] = []
    original = SessionActor._cancel

    async def recorded(self, reason):
        cancels.append((self.session_id, reason))
        await original(self, reason)

    monkeypatch.setattr(SessionActor, "_cancel", recorded)
    # 在工作线程里物化并收回，期间故意堵住事件循环：``start`` 排在循环上、此刻还没执行。
    done = threading.Event()

    def recall() -> None:
        spaces.get_or_create(child_id).actor.request_recall()
        done.set()

    threading.Thread(target=recall).start()
    assert done.wait(5)  # noqa: ASYNC - 有意阻塞事件循环
    await _settle(child_id)

    assert cancels == [(child_id, "parent")]


async def test_one_derived_session_failing_does_not_spare_its_siblings(monkeypatch):
    """B4：收回逐个派生会话，一个失败（并发删除、写锁超时）不影响其余的被收回。"""
    recalled: list[int] = []

    class _Actor:
        def __init__(self, session_id):
            self.session_id = session_id

        def request_recall(self):
            if self.session_id == 1:
                raise KeyError(1)
            recalled.append(self.session_id)

    monkeypatch.setattr(
        session_recall_module.service_depends,
        "get_session_service",
        lambda: SimpleNamespace(
            list_derived_sessions=lambda _sid: [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        ),
    )
    from app.session_runtime import session_runtime_space_registry as registry_module

    monkeypatch.setattr(
        registry_module.session_runtime_spaces,
        "get_or_create",
        lambda sid: SimpleNamespace(actor=_Actor(sid)),
    )
    await SessionRecall(99).recall()
    assert recalled == [2]


async def test_only_derived_sessions_can_be_recalled(derived):
    """B5：顶层会话没有派生方，收回被拒绝，这一行也不留下截止、照常可读。"""
    import pytest

    with pytest.raises(ValueError):
        await asyncio.to_thread(depends.get_session_service().mark_recalled, derived.parent.id)
    assert depends.get_session_service().get_session(derived.parent.id).extra.recalled_through_run_id is None


async def test_a_session_recalled_before_its_first_run_does_not_start(derived, monkeypatch):
    """第 6 条：委派刚投完任务说明、子 Session 的第一个 Run 还没开起来就被收回：任务说明不能再开出 Run。

    屏障放在子 Session 的推进入口（进闸门之前），收回先拿到闸门。"""
    derived.model = _Model("answer")
    held, release = asyncio.Event(), asyncio.Event()
    advance = SessionActor._advance

    async def gated_advance(self, woken_by):
        if self.session_id != derived.parent.id and not release.is_set():
            held.set()
            await release.wait()
        return await advance(self, woken_by)

    cancelled = asyncio.Event()
    cancel = SessionActor._cancel

    async def recorded_cancel(self, reason):
        await cancel(self, reason)
        cancelled.set()

    monkeypatch.setattr(SessionActor, "_advance", gated_advance)
    monkeypatch.setattr(SessionActor, "_cancel", recorded_cancel)
    _send(derived.parent.id, derived.model_config.id, "start")
    await asyncio.wait_for(held.wait(), 8)
    child_id = _child_id(derived.parent.id)
    await asyncio.to_thread(spaces.get_or_create(child_id).actor.request_recall)
    await asyncio.wait_for(cancelled.wait(), 8)
    release.set()
    await _settle(child_id, derived.parent.id)
    await asyncio.sleep(0.2)
    await _settle(child_id, derived.parent.id)

    assert [run.end_reason for run in _runs(child_id)] in ([], ["parent_cancelled"])
    assert len(_runs(derived.parent.id)) == 1
    assert _pending_kinds(derived.parent.id) == []


async def test_a_session_derived_while_its_deriver_is_being_recalled_is_recalled_too(derived, monkeypatch):
    """第 5 条：C 的 ``delegate_task`` 查过取消信号、还没建出 G 时，C 被收回并列完了派生会话。G 建出来之后
    也得被收回：不然它照常跑完、向 C 汇报，C 据此开出截止之后的 Run 又向 P 汇报。"""
    from app.service.session.session_service import SessionService

    derived.model = _ThreeLevelModel()
    derived.model.grand_gate.set()  # G 一开跑就答完
    creating, release = threading.Event(), threading.Event()
    create = SessionService.create_session

    def gated_create(self, *args, **kwargs):
        if kwargs.get("title") == "grand":
            creating.set()
            assert release.wait(10), "create_session was never released"
        return create(self, *args, **kwargs)

    listed = threading.Event()
    child: list[int] = []
    recall_derived = SessionRecall._recall_derived

    def recorded_recall(self):
        recall_derived(self)
        if child and self.session_id == child[0]:
            listed.set()

    monkeypatch.setattr(SessionService, "create_session", gated_create)
    monkeypatch.setattr(SessionRecall, "_recall_derived", recorded_recall)
    try:
        _send(derived.parent.id, derived.model_config.id, "start")
        assert await asyncio.to_thread(creating.wait, 8)
        child.append(_child_id(derived.parent.id))
        await asyncio.to_thread(spaces.get_or_create(child[0]).actor.request_recall)
        assert await asyncio.to_thread(listed.wait, 8)
    finally:
        release.set()
    await _until(child[0], lambda: depends.get_session_service().list_derived_sessions(child[0]))
    grand_id = _child_id(child[0])
    for _ in range(3):
        await _settle(grand_id, child[0], derived.parent.id)
        await asyncio.sleep(0.1)

    assert all(run.end_reason == "parent_cancelled" for run in _runs(grand_id)), [
        (run.status, run.end_reason) for run in _runs(grand_id)
    ]
    assert len(_runs(derived.parent.id)) == 1, [run.final_output for run in _runs(derived.parent.id)]
    assert _pending_kinds(derived.parent.id) == []
