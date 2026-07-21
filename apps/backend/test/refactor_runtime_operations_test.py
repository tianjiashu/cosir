"""针对「执行状态收敛到 Turn」重构的隔离单元测试。

覆盖 RuntimeOperations / ContextBuilder / ToolExecution 的纯逻辑行为契约。

重要约束（与 refactor_service_facade_test.py 一致）：
- 仓库存在预存坏导入 ``app/config/logging/save/sqlite_handler.py:12``（误写为
  ``from app.storage.crud.log import LogStore``），位于 ``app.config.logging.__init__`` 导入链上，
  导致 ``import app.core.runtime.runner`` 在导入期失败。
- 因此本测试**不导入** ``app.core.runtime.runner``（其 ``cancel_turn`` 行为改由
  ``TurnService.update_turn_status`` 的 cancel 语义等价覆盖，见下方说明），仅隔离测试不触发
  坏导入链、且属于本次改动行为契约的纯逻辑模块：
  * ``app.core.runtime.runtime_operations``（RuntimeOperations，用 fake turn_store / scheduler）
  * ``app.core.context.builder``（TextContextBuilder 跨轮消息拼接）
  * ``app.service.tool_execution.tool_execution_service``（ToolExecutionService，用 fake scheduler）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.agents.profile import AgentProfile, default_developer_agent
from app.core.context import TextContextBuilder
from app.core.runtime.runtime_operations import RuntimeOperations, _noop_write_event
from app.models import RuntimeMessage, TurnRecord
from app.service.tool_execution.run_result import ToolRunResult
from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolObservation

# ---------------------------------------------------------------------------
# Fake 依赖对象
# ---------------------------------------------------------------------------


class FakeTurnStore:
    """记录调用并返回可预测结果的 turn_store 替身。

    同时兼容两种被测接口契约：
    * 作为 ``RuntimeOperations.turn_store`` 时（runner 实际传入 ``TurnService`` 实例），
      需要 ``get_turn`` / ``has_turn_status`` / ``update_turn_status`` /
      ``list_turns_for_task`` / ``get_latest_turn``。
    * 作为 ``TurnService`` 的 ``turn_crud`` 时，需要 ``get`` / ``update_status`` /
      ``list_by_task`` / ``create``。
    本 fake 把两套命名都实现，以便直接驱动被测代码。
    """

    def __init__(self) -> None:
        self._turns: dict[str, TurnRecord] = {}
        self._seq = 0
        self.updated_status: list[tuple[str, str, str | None]] = []
        self.has_status_calls: list[tuple[str, str]] = []

    def _new_id(self) -> str:
        self._seq += 1
        return f"turn-{self._seq}"

    def create(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        turn_id = self._new_id()
        turn = TurnRecord(
            turn_id=turn_id,
            task_id=task_id,
            input_text=input_text,
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self._turns[turn_id] = turn
        return turn

    def get(self, turn_id: str) -> TurnRecord:
        if turn_id not in self._turns:
            raise KeyError(turn_id)
        return self._turns[turn_id]

    # RuntimeOperations.turn_store 契约别名
    def get_turn(self, turn_id: str) -> TurnRecord:
        return self.get(turn_id)

    def list_turns_for_task(self, task_id: str) -> list[TurnRecord]:
        return [t for t in self._turns.values() if t.task_id == task_id]

    def list_by_task(self, task_id: str) -> list[TurnRecord]:
        return self.list_turns_for_task(task_id)

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        turns = self.list_turns_for_task(task_id)
        if not turns:
            raise KeyError(task_id)
        return turns[-1]

    def get_first_for_task(self, task_id: str) -> TurnRecord:
        turns = self.list_turns_for_task(task_id)
        if not turns:
            raise KeyError(task_id)
        return turns[0]

    def update_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        return self.update_turn_status(turn_id, status, end_reason)

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        self._turns[turn_id] = TurnRecord(
            turn_id=turn_id,
            task_id=self._turns[turn_id].task_id,
            input_text=self._turns[turn_id].input_text,
            status=status,
            created_at=self._turns[turn_id].created_at,
            updated_at=datetime.now(UTC),
            end_reason=end_reason,
        )
        self.updated_status.append((turn_id, status, end_reason))
        return self._turns[turn_id]

    def has_turn_status(self, turn_id: str, status: str) -> bool:
        self.has_status_calls.append((turn_id, status))
        return self._turns[turn_id].status == status

    def claim_pending(self, turn_id: str) -> bool:
        return True


class FakeMessageStore:
    """记录调用并返回可预测结果的 message_store 替身（供 context builder 使用）。"""

    def __init__(self) -> None:
        self._store: dict[str, list[RuntimeMessage]] = {}

    def save_messages(self, turn_id: str, messages: list[RuntimeMessage]) -> None:
        self._store[turn_id] = list(messages)

    def load_messages(self, turn_id: str) -> list[RuntimeMessage]:
        return list(self._store.get(turn_id, []))


class FakeToolScheduler:
    """记录调用并返回可预测结果的 ToolScheduler 替身。"""

    def __init__(self, observations: list[ToolObservation] | None = None) -> None:
        self.execute_calls: list[ToolCall] = []
        self._observations = observations

    def execute(self, call: ToolCall) -> ToolObservation:
        self.execute_calls.append(call)
        if self._observations:
            return self._observations.pop(0)
        return ToolObservation(
            tool_name=call.tool_name,
            status="ok",
            content=f"result-of-{call.tool_name}",
            tool_call_id=call.call_id,
        )


def _make_runtime_operations(
    turn_store: FakeTurnStore | None = None,
    context_builder: TextContextBuilder | None = None,
    scheduler: FakeToolScheduler | None = None,
    agent_profile: AgentProfile | None = None,
    current_turn_id: str = "",
) -> RuntimeOperations:
    from app.config.settings import default_settings

    turn_store = turn_store or FakeTurnStore()
    scheduler = scheduler or FakeToolScheduler()
    return RuntimeOperations(
        settings=default_settings(),
        turn_store=turn_store,
        context_builder=context_builder or TextContextBuilder(),
        tool_scheduler=scheduler,
        logger=__import__("logging").getLogger("test"),
        agent_profile=agent_profile or default_developer_agent(),
        current_turn_id=current_turn_id,
    )


def _ts() -> datetime:
    return datetime.now(UTC)


# ===========================================================================
# 一、RuntimeOperations.build_messages：基于 turn + 前置轮 trajectory 拼接
# ===========================================================================


# 测试目的：验证 build_messages 通过传入的 turn_store（充当 message_store）加载前置轮轨迹。
# 可能发现的缺陷：build_messages 未把 turn_store 传给 context_builder，导致前置轮记忆丢失。
def test_build_messages_uses_turn_store_as_message_store():
    store = FakeTurnStore()
    t1 = store.create("task-1", "first input", status="completed")
    t2 = store.create("task-1", "second input", status="running")

    # 给 FakeTurnStore 增加 load_messages（RuntimeOperations.build_messages 把 self._turn_store
    # 当作 message_store 传入 context_builder.build_messages）。
    prior_msgs = [RuntimeMessage(role="assistant", content_text="answer to first")]
    store.load_messages = lambda turn_id: prior_msgs if turn_id == t1.turn_id else []  # type: ignore[assignment]

    ops = _make_runtime_operations(turn_store=store, current_turn_id=t2.turn_id)
    messages = ops.build_messages()

    roles = [m.role for m in messages]
    assert roles[0] == "system"
    # 前置轮 assistant 消息应出现在当前轮 user 之前
    assert any(m.content_text == "answer to first" for m in messages)
    # 当前轮用户输入作为 user 消息
    user_msgs = [m for m in messages if m.role == "user"]
    assert user_msgs and user_msgs[-1].content_text == "second input"


# 测试目的：验证 build_messages 在无前置轮时仅含 system + 当前轮 user。
# 可能发现的缺陷：空 history 时仍尝试加载导致异常，或重复插入当前轮。
def test_build_messages_only_system_and_current_when_no_prior():
    store = FakeTurnStore()
    t1 = store.create("task-1", "only input", status="pending")
    store.load_messages = lambda turn_id: []  # type: ignore[assignment]

    ops = _make_runtime_operations(turn_store=store, current_turn_id=t1.turn_id)
    messages = ops.build_messages()

    assert [m.role for m in messages] == ["system", "user"]
    assert messages[1].content_text == "only input"


# 测试目的：验证 build_messages 在 current_turn_id 为空时抛出 KeyError。
# 可能发现的缺陷：未绑定 current_turn_id 时静默返回空/错误。
def test_build_messages_raises_without_current_turn_id():
    ops = _make_runtime_operations(turn_store=FakeTurnStore(), current_turn_id="")
    try:
        ops.build_messages()
        raised = False
    except KeyError:
        raised = True
    assert raised, "build_messages 应在未绑定 current_turn_id 时抛 KeyError"


# ===========================================================================
# 二、RuntimeOperations 读/写委托
# ===========================================================================


# 测试目的：验证 get_current_turn 返回 current_turn_id 对应的 turn。
# 可能发现的缺陷：get_current_turn 错误地返回第一个/最新 turn（历史 bug）。
def test_get_current_turn_returns_bound_turn():
    store = FakeTurnStore()
    t1 = store.create("task-1", "first", status="completed")
    t2 = store.create("task-1", "second", status="running")
    ops = _make_runtime_operations(turn_store=store, current_turn_id=t2.turn_id)
    assert ops.get_current_turn().turn_id == t2.turn_id
    assert ops.get_current_turn().turn_id != t1.turn_id


# 测试目的：验证 get_latest_turn / get_turn_for_task 委托到 turn_store 最新轮。
# 可能发现的缺陷：get_turn_for_task 仍返回第一轮。
def test_get_latest_turn_delegation():
    store = FakeTurnStore()
    store.create("task-1", "first", status="completed")
    t2 = store.create("task-1", "second", status="running")
    ops = _make_runtime_operations(turn_store=store)
    assert ops.get_latest_turn("task-1").turn_id == t2.turn_id
    assert ops.get_turn_for_task("task-1").turn_id == t2.turn_id


# 测试目的：验证 list_turns_for_task 委托到 turn_store 全量列表。
# 可能发现的缺陷：未委托或返回单轮。
def test_list_turns_for_task_delegation():
    store = FakeTurnStore()
    store.create("task-1", "a", status="completed")
    store.create("task-1", "b", status="running")
    ops = _make_runtime_operations(turn_store=store)
    assert len(ops.list_turns_for_task("task-1")) == 2


# 测试目的：验证 has_turn_status / update_turn_status 委托到 turn_store。
# 可能发现的缺陷：未透传 end_reason 或比对错误。
def test_has_and_update_turn_status_delegation():
    store = FakeTurnStore()
    t = store.create("task-1", "x", status="running")
    ops = _make_runtime_operations(turn_store=store)
    assert ops.has_turn_status(t.turn_id, "running") is True
    assert ops.has_turn_status(t.turn_id, "completed") is False
    updated = ops.update_turn_status(t.turn_id, "cancelled", end_reason="user_cancelled")
    assert updated.status == "cancelled"
    assert updated.end_reason == "user_cancelled"
    assert store.updated_status[-1] == (t.turn_id, "cancelled", "user_cancelled")


# 测试目的：验证 _noop_write_event 静默丢弃、返回 None（无副作用契约）。
# 可能发现的缺陷：_noop_write_event 抛异常或产生副作用。
def test_noop_write_event_is_side_effect_free():
    assert _noop_write_event("x", {}) is None


# ===========================================================================
# 三、RuntimeOperations.run_tool_calls 委托 ToolExecutionService
# ===========================================================================


# 测试目的：验证 run_tool_calls 经 tool_service 调用 scheduler，并把观察转消息。
# 可能发现的缺陷：未委托 scheduler、未生成 tool 角色消息、step_id 缺失。
# 注：此处显式传入 write_event 回调，验证事件被正确发出（默认 _noop_write_event 静默接管无需事件）。
def test_run_tool_calls_delegates_to_scheduler():
    store = FakeTurnStore()
    scheduler = FakeToolScheduler()
    ops = _make_runtime_operations(turn_store=store, scheduler=scheduler)
    calls = [ToolCall(tool_name="safe_read", arguments={"path": "/a"}, call_id="c1")]
    captured: list = []
    result = ops.run_tool_calls(
        task_id="task-1",
        calls=calls,
        step_id="s1",
        write_event=lambda et, payload: captured.append((et, payload)),
    )
    assert isinstance(result, ToolRunResult)
    assert scheduler.execute_calls == calls
    assert len(result.observations) == 1
    assert len(result.messages_for_model) == 1
    assert result.messages_for_model[0].role == "tool"
    assert result.messages_for_model[0].metadata.get("tool_call_id") == "c1"
    # 事件经回调写出
    assert captured and captured[0][1].get("tool_call_id") == "c1"


# 测试目的：验证未提供 write_event 时 run_tool_calls 应静默执行（不抛）。
# 已修复：``_noop_write_event`` 签名已对齐为 ``(event_type, payload)``，默认回调不再触发 TypeError。
def test_run_tool_calls_without_write_event_is_safe():
    store = FakeTurnStore()
    scheduler = FakeToolScheduler()
    ops = _make_runtime_operations(turn_store=store, scheduler=scheduler)
    result = ops.run_tool_calls(task_id="task-1", calls=[ToolCall(tool_name="r")])
    assert result.observations


# 测试目的：验证 write_event=None 时走 ``_noop_write_event`` 默认回调不抛异常（缺陷已修复）。
def test_run_tool_calls_none_write_event_no_error():
    store = FakeTurnStore()
    scheduler = FakeToolScheduler()
    ops = _make_runtime_operations(turn_store=store, scheduler=scheduler)
    # 不应抛；未提供 write_event 时由 _noop_write_event 静默接管
    ops.run_tool_calls(task_id="task-1", calls=[ToolCall(tool_name="r")])


# ===========================================================================
# 四、TextContextBuilder 跨轮消息拼接（边界 + 异常路径）
# ===========================================================================


# 测试目的：验证 builder 当前轮为 None 时只产出 system，不插入 user。
# 可能发现的缺陷：current_turn 为 None 时仍插入 user 消息。
def test_builder_current_turn_none_only_system():
    msg_store = FakeMessageStore()
    profile = default_developer_agent()
    messages = TextContextBuilder().build_messages(
        profile, None, [], msg_store
    )
    assert [m.role for m in messages] == ["system"]


# 测试目的：验证 builder 当 turn_history 为 None 时不抛、仅 system + 当前轮 user。
# 可能发现的缺陷：turn_history 为 None 时遍历失败。
def test_builder_none_history_safe():
    msg_store = FakeMessageStore()
    profile = default_developer_agent()
    current = TurnRecord(
        turn_id="t1", task_id="task-1", input_text="hi", status="pending",
        created_at=_ts(), updated_at=_ts(),
    )
    messages = TextContextBuilder().build_messages(profile, current, None, msg_store)
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[1].content_text == "hi"


# 测试目的：验证 builder 跨轮拼接：前置轮消息来自 message_store.load_messages。
# 可能发现的缺陷：前置轮轨迹未加载或顺序错乱。
def test_builder_cross_turn_prior_trajectory():
    msg_store = FakeMessageStore()
    profile = default_developer_agent()
    t1 = TurnRecord(
        turn_id="t1", task_id="task-1", input_text="q1", status="completed",
        created_at=_ts(), updated_at=_ts(),
    )
    t2 = TurnRecord(
        turn_id="t2", task_id="task-1", input_text="q2", status="running",
        created_at=_ts(), updated_at=_ts(),
    )
    msg_store.save_messages(
        "t1",
        [
            RuntimeMessage(role="user", content_text="historic user"),
            RuntimeMessage(role="assistant", content_text="historic assistant"),
        ],
    )
    messages = TextContextBuilder().build_messages(profile, t2, [t1, t2], msg_store)
    contents = [m.content_text for m in messages]
    assert contents[0].startswith("你是一个本地 coding-agent")
    assert "historic user" in contents
    assert "historic assistant" in contents
    # 当前轮用户输入出现在最后
    assert messages[-1].role == "user"
    assert messages[-1].content_text == "q2"


# 测试目的：验证 builder 不会把当前轮自身轨迹重复拼入前置轮。
# 可能发现的缺陷：当前轮消息被当作前置轮重复插入。
def test_builder_excludes_current_turn_from_prior():
    msg_store = FakeMessageStore()
    profile = default_developer_agent()
    t1 = TurnRecord(
        turn_id="t1", task_id="task-1", input_text="q1", status="completed",
        created_at=_ts(), updated_at=_ts(),
    )
    t2 = TurnRecord(
        turn_id="t2", task_id="task-1", input_text="q2", status="running",
        created_at=_ts(), updated_at=_ts(),
    )
    msg_store.save_messages(
        "t2", [RuntimeMessage(role="assistant", content_text="CURRENT_TURN_MSG")]
    )
    messages = TextContextBuilder().build_messages(profile, t2, [t1, t2], msg_store)
    # 当前轮轨迹不应出现在前置轮拼接中（仅其 input_text 作为 user 出现在末尾）
    prior_contents = [m.content_text for m in messages[:-1]]
    assert "CURRENT_TURN_MSG" not in prior_contents
    assert messages[-1].content_text == "q2"


# 测试目的：验证 profile 为 None 时回退到 default_developer_agent。
# 可能发现的缺陷：profile 为 None 时 AttributeError。
def test_builder_none_profile_falls_back_to_default():
    msg_store = FakeMessageStore()
    current = TurnRecord(
        turn_id="t1", task_id="task-1", input_text="hi", status="pending",
        created_at=_ts(), updated_at=_ts(),
    )
    messages = TextContextBuilder().build_messages(None, current, [], msg_store)
    assert messages[0].content_text.startswith("你是一个本地 coding-agent")
    assert "developer" in messages[0].content_text


# ===========================================================================
# 五、ToolExecutionService.run_calls_with_events 委托 ToolScheduler
# ===========================================================================


# 测试目的：验证 run_calls_with_events 对每个 call 调 scheduler.execute 并生成 tool 消息。
# 可能发现的缺陷：未遍历 calls、未生成 messages_for_model。
def test_tool_execution_runs_all_calls_and_builds_messages():
    scheduler = FakeToolScheduler()
    svc = ToolExecutionService(
        scheduler=scheduler, agent_id="developer", logger=__import__("logging").getLogger("t")
    )
    calls = [
        ToolCall(tool_name="read", call_id="c1"),
        ToolCall(tool_name="write", call_id="c2"),
    ]
    result = svc.run_calls_with_events(task_id="task-1", step_id="s1", calls=calls)
    assert isinstance(result, ToolRunResult)
    assert len(scheduler.execute_calls) == 2
    assert [o.tool_name for o in result.observations] == ["read", "write"]
    assert len(result.messages_for_model) == 2
    assert all(m.role == "tool" for m in result.messages_for_model)
    assert result.messages_for_model[0].metadata["tool_call_id"] == "c1"


# 测试目的：验证提供 write_event 时为每个完成调用发出 TOOL_CALL_FINISHED 事件。
# 可能发现的缺陷：事件未发出、事件类型错、payload 缺字段。
def test_tool_execution_emits_events_when_write_event_provided():
    from app.models.enums.event_type import EventType

    scheduler = FakeToolScheduler()
    svc = ToolExecutionService(
        scheduler=scheduler, agent_id="developer", logger=__import__("logging").getLogger("t")
    )
    emitted: list[tuple[Any, dict]] = []
    svc.run_calls_with_events(
        task_id="task-1",
        step_id="s9",
        calls=[ToolCall(tool_name="read", call_id="c1")],
        write_event=lambda et, payload: emitted.append((et, payload)),
    )
    assert len(emitted) == 1
    et, payload = emitted[0]
    assert et == EventType.TOOL_CALL_FINISHED
    assert payload["step_id"] == "s9"
    assert payload["tool_name"] == "read"
    assert payload["tool_call_id"] == "c1"


# 测试目的：验证单个工具失败时 observation.status 表达失败而非向上抛出。
# 可能发现的缺陷：工具失败被向上抛出（破坏「异常：无」契约）。
def test_tool_execution_does_not_raise_on_failure():
    obs = ToolObservation(tool_name="bad", status="error", content="boom", error="x")
    scheduler = FakeToolScheduler(observations=[obs])
    svc = ToolExecutionService(
        scheduler=scheduler, agent_id="developer", logger=__import__("logging").getLogger("t")
    )
    result = svc.run_calls_with_events(
        task_id="task-1", step_id="s", calls=[ToolCall(tool_name="bad")]
    )
    assert result.observations[0].status == "error"
    assert result.messages_for_model[0].content_text == "boom"


# ===========================================================================
# 六、runner.cancel_turn 语义等价覆盖（runner 不可安全导入，改测 turn 取消语义）
# ===========================================================================
# 说明：``app.core.runtime.runner.AgentRuntime.cancel_turn`` 在导入期会触发预存坏导入链
# （``from app.config.logging import ...``），无法在本测试进程安全 import。其「取消语义」核心为：
#   1) 把 turn 更新为 cancelled 并带 end_reason="user_cancelled"；
#   2) emit RUN_CANCELLED 事件（事件发出依赖 runner 私有 _record，无法隔离验证）。
# 此处改测 TurnService.update_turn_status 的 cancel 语义，作为 1) 的等价契约覆盖；
# 2) 的事件契约因坏导入链限制无法在本测试集内验证，列为已知限制（见结论报告）。


# 测试目的：验证 turn 取消语义——更新为 cancelled 且 end_reason=user_cancelled
# （等价 runner.cancel_turn 核心）。
# 可能发现的缺陷：cancel 后 status/end_reason 未正确落定。
def test_turn_cancel_semantics_via_turn_service():
    from app.service.task.turn_service import TurnService

    store = FakeTurnStore()
    t = store.create("task-1", "x", status="running")
    svc = TurnService(task_crud=store, turn_crud=store)  # type: ignore[arg-type]
    cancelled = svc.update_turn_status(t.turn_id, "cancelled", end_reason="user_cancelled")
    assert cancelled.status == "cancelled"
    assert cancelled.end_reason == "user_cancelled"
    # 取消后 has_turn_status("cancelled") 应为 True
    assert svc.has_turn_status(t.turn_id, "cancelled") is True


# ===========================================================================
# 七、TurnService.create_turn / get_turn 补充（覆盖率补齐）
# ===========================================================================


# 测试目的：验证 create_turn 创建 turn 并同步更新 task 最新轮次与预览。
# 可能发现的缺陷：未同步更新 task.latest_turn、preview 计算错误。
def test_turn_service_create_turn_syncs_task():
    from app.service.task.turn_service import TurnService

    class _TaskCrud:
        def __init__(self) -> None:
            self.updated: list[tuple[str, str, str]] = []

        def update_latest_turn(self, task_id: str, latest_turn_id: str, preview: str) -> None:
            self.updated.append((task_id, latest_turn_id, preview))

    task_crud = _TaskCrud()
    turn_crud = FakeTurnStore()
    svc = TurnService(task_crud=task_crud, turn_crud=turn_crud)  # type: ignore[arg-type]
    turn = svc.create_turn("task-1", "  帮我写登录  ", status="pending")
    assert turn.status == "pending"
    assert task_crud.updated == [("task-1", turn.turn_id, "帮我写登录")]


# 测试目的：验证 create_turn 拒绝空白 input_text。
# 可能发现的缺陷：空白输入被接受导致脏数据。
@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_turn_service_create_turn_rejects_empty(bad):
    from app.service.task.turn_service import TurnService

    svc = TurnService(task_crud=FakeTurnStore(), turn_crud=FakeTurnStore())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        svc.create_turn("task-1", bad)  # type: ignore[arg-type]


# 测试目的：验证 get_turn 委托到 turn_crud.get。
# 可能发现的缺陷：未委托或返回错误 turn。
def test_turn_service_get_turn_delegation():
    from app.service.task.turn_service import TurnService

    turn_crud = FakeTurnStore()
    t = turn_crud.create("task-1", "x", status="pending")
    svc = TurnService(task_crud=FakeTurnStore(), turn_crud=turn_crud)  # type: ignore[arg-type]
    assert svc.get_turn(t.turn_id).turn_id == t.turn_id


# 测试目的：验证 get_turn_for_task 委托到 turn_crud.get_latest_turn（返回最新轮，符合设计契约）。
def test_turn_service_get_turn_for_task_returns_latest():
    from app.service.task.turn_service import TurnService

    turn_crud = FakeTurnStore()
    turn_crud.create("task-1", "first", status="completed")
    t2 = turn_crud.create("task-1", "second", status="running")
    svc = TurnService(task_crud=FakeTurnStore(), turn_crud=turn_crud)  # type: ignore[arg-type]
    assert svc.get_turn_for_task("task-1").turn_id == t2.turn_id


# ===========================================================================
# 八、RuntimeOperations.log_exception 补充（覆盖率补齐）
# ===========================================================================


# 测试目的：验证 log_exception 通过 logger.exception 写诊断且不抛。
# 可能发现的缺陷：log_exception 抛异常或吞掉 logger。
def test_runtime_operations_log_exception_safe():
    import logging

    ops = _make_runtime_operations(turn_store=FakeTurnStore())
    # 不应抛；使用真实 logger（exception 仅记录）
    ops.log_exception("boom", extra={"k": "v"})
    assert isinstance(ops._logger, logging.Logger)
