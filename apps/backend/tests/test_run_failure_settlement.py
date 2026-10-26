"""异常逃逸时的 Run 终态收口契约测试。

本模块锁定核心不变量：**任何从 workflow 逃逸的异常都必须由 ``run_driver.drive_run`` 的
异常边界当场把 Run 落定为 failed**。历史缺陷是 workflow / runner / executor 三方都认为
「终态由对方落定」，实际无人落定，导致 provider 报错后 Run 永久停留在 ``running``、前端既
不显示失败原因也无法开始新轮次。

收口细节（当前实现）：

- 落定直接走 run 级状态服务（``transition(FAIL)``，条件更新），**不依赖**运行上下文是否装配
  成功——装配失败时同样能落定，正是这一点覆盖了「workflow 还没进入就失败」；
- 分类失败 code、写受控文案、把模型响应体 ``message`` 作为受控 UI 消息一并落库；
- 落定完成后异常**不再上抛**；落定自身失败时异常上抛到执行器记录，Run 留在 running，由下次
  启动的 ``recover_orphaned_runs`` 收敛。

同时覆盖配套的受控错误投影：终态事件携带 ``error``、冷重建从持久化契约投影 ``error``、
快照校验拒绝被污染的 ``error``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_state_activity import ConversationStateActivity
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.config.constant import Constant
from app.core.events import RunStatusChangedEvent
from app.core.workflow.inbox.run_mailbox import RunMailbox
from app.core.workflow.workflow_run_failure import WorkflowRunFailure
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus, RunTransition
from app.models.enums.error_kind import ErrorKind
from app.core import run_driver

_QUOTA_CODE = ErrorKind.MODEL_INSUFFICIENT_QUOTA.value
_SERVICE_ERROR_CODE = ErrorKind.MODEL_SERVICE_ERROR.value


class _ConnectFailure(Exception):
    """等价于 provider 连接失败异常的替身：类型名含 connect，且保留 HTTP 响应体。"""

    def __init__(self) -> None:
        super().__init__("connection refused")
        self.body = {"error": {"message": "connection refused"}}


class _TimeoutFailure(Exception):
    """等价于 provider 超时异常的替身：类型名含 timeout，无响应体。"""


class _RecordingRunStateService:
    """记录 ``transition(FAIL)`` 入参的假 run 级状态服务。

    覆盖三种落定结果：正常落定、条件更新落空（Run 已被别处落终态）、落定自身失败。
    """

    def __init__(self, *, settles: bool = True, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._settles = settles
        self._failure = failure

    def transition(
        self,
        run_id: int,
        transition: RunTransition,
        *,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: object = None,
        error_message: str | None = None,
    ) -> SimpleNamespace | None:
        """记录落定入参；``failure`` 非空时抛出，``settles`` 为假时返回 None（竞态落空）。"""

        self.calls.append(
            {
                "run_id": run_id,
                "end_reason": end_reason,
                "final_output": final_output,
                "usage_stats": usage_stats,
                "error_message": error_message,
            }
        )
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(id=run_id, task_id=3) if self._settles else None


def _running_state() -> ConversationStateSnapshot:
    """构造只含一个 running Run 的合法 snapshot（含新的 ``error`` 键）。"""

    state = empty_snapshot()
    state["activity"] = ConversationStateActivity(
        runId=1,
        status="running",
        endReason=None,
        error=None,
        usage=None,
        contextWindowTotal=None,
        launch={"modelConfigId": None, "reasoningEffort": None, "banTools": []},
    )
    return state


def _error_mutation_values(
    state: ConversationStateSnapshot, event: RunStatusChangedEvent
) -> list[object]:
    """取出事件 plan 中对 Run ``error`` 写入的全部取值。"""

    mutations = plan_event(event, state)
    return [mutation.value for mutation in mutations if mutation.path == ("activity", "error")]


def test_failure_code_for_classifies_timeout() -> None:
    """超时异常归类为模型超时码。"""

    assert run_driver._failure_code_for(_TimeoutFailure()) == ErrorKind.MODEL_TIMEOUT.value


def test_failure_code_for_classifies_connection_error() -> None:
    """连接失败归类为网络错误，而不是笼统的图执行失败。"""

    assert run_driver._failure_code_for(_ConnectFailure()) == ErrorKind.MODEL_NETWORK_ERROR.value


def test_failure_code_for_falls_back_to_graph_failed() -> None:
    """无法归类为模型错误的异常使用中性兜底 code，而不是猜测成模型问题。"""

    assert (
            run_driver._failure_code_for(RuntimeError("node exploded"))
            == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
    )


def test_failure_code_for_honours_workflow_failure_code() -> None:
    """工作流已判定失败语义时直接采用其自带 code，不做二次猜测。"""

    failure = WorkflowRunFailure(
        Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE, "运行期模型解析失败"
    )

    assert (
            run_driver._failure_code_for(failure)
            == Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE
    )


def _make_agent(monkeypatch: pytest.MonkeyPatch, workflow_error: BaseException) -> int:
    """让 workflow 抛出给定异常，返回待驱动的 run_id。"""

    class _StubWorkflow:
        async def run(self, *_args: object, **_kwargs: object) -> None:
            raise workflow_error

    monkeypatch.setattr(
        "app.core.workflow.workflow.ReactLikeWorkflow", _StubWorkflow
    )
    return 7


async def _no_initial_delivery() -> None:
    return None


def _drive_run(
    run_id: int, initial_delivery: Callable[[], Awaitable[None]] = _no_initial_delivery
) -> Awaitable[None]:
    """以空 mailbox 驱动；初始投递默认为空操作，单测按需替换。"""
    return run_driver.drive_run(run_id, RunMailbox(), initial_delivery)


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    operations: object | None,
    *,
    build_operations_error: BaseException | None = None,
    run_state_service: _RecordingRunStateService | None = None,
) -> SimpleNamespace:
    """把 ``drive_run`` 依赖的 service、hook 与观测替换为最小替身，只观察「异常 → 分类 → 落定」。

    ``build_operations_error`` 非空时模拟门面尚未构造就失败的路径。
    """
    state_service = run_state_service or _RecordingRunStateService()

    tasks = SimpleNamespace(get_task=lambda task_id: SimpleNamespace(id=task_id, workspace_id=9))
    workspaces = SimpleNamespace(get_workspace=lambda ws_id: SimpleNamespace(id=ws_id))
    monkeypatch.setattr(run_driver, "get_task_service", lambda: tasks)
    monkeypatch.setattr(run_driver, "get_workspace_service", lambda: workspaces)
    monkeypatch.setattr(
        run_driver,
        "get_conversation_run_ledger",
        lambda: SimpleNamespace(
            get_run=lambda run_id: SimpleNamespace(id=run_id, task_id=3),
            transition=state_service.transition,
        ),
    )

    async def _noop_fire(_context: object) -> None:
        return None

    def _build(*_args: object, **_kwargs: object) -> object:
        if build_operations_error is not None:
            raise build_operations_error
        return operations

    monkeypatch.setattr(run_driver.HookInterceptor, "async_safe_fire", _noop_fire)
    monkeypatch.setattr(run_driver, "_build_context", _build)
    monkeypatch.setattr(run_driver, "_runtime_config", lambda *_args: object())
    monkeypatch.setattr(
        run_driver,
        "get_conversation_event_projector",
        lambda: SimpleNamespace(process=lambda _event: None),
    )
    return SimpleNamespace(run_agent=_drive_run)


@pytest.mark.asyncio
async def test_run_agent_settles_failed_run_without_propagating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workflow 抛 provider 异常时，Run 必须当场落 failed，且异常不再上抛。

    落定入参同时锁定三件事：分类出的失败 code、恒为受控文案的 ``final_output``，以及
    从响应体提取、原样透传的 ``error_message``。
    """

    run_state_service = _RecordingRunStateService()
    original = _ConnectFailure()
    runtime = _make_runtime(monkeypatch, object(), run_state_service=run_state_service)

    await runtime.run_agent(_make_agent(monkeypatch, original))

    network_error_code = ErrorKind.MODEL_NETWORK_ERROR.value
    assert run_state_service.calls == [
        {
            "run_id": 7,
            "end_reason": network_error_code,
            "final_output": run_failure_message(network_error_code),
            "usage_stats": None,
            "error_message": "connection refused",
        }
    ]


@pytest.mark.asyncio
async def test_run_agent_tolerates_lost_settlement_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 已被其它路径落终态（条件更新落空）时正常返回，不抛异常。"""

    run_state_service = _RecordingRunStateService(settles=False)
    runtime = _make_runtime(monkeypatch, object(), run_state_service=run_state_service)

    await runtime.run_agent(_make_agent(monkeypatch, RuntimeError("node exploded")))

    assert len(run_state_service.calls) == 1


@pytest.mark.asyncio
async def test_run_agent_settles_even_when_operations_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """门面尚未构造就失败时仍要落定：落定走 run 级状态服务，不依赖门面。

    这条路径正是「workflow 还没进入就失败」的覆盖点，若不落定就会留下僵尸 running。
    """

    run_state_service = _RecordingRunStateService()
    original = RuntimeError("operations unavailable")
    runtime = _make_runtime(
        monkeypatch, None, build_operations_error=original, run_state_service=run_state_service
    )

    await runtime.run_agent(_make_agent(monkeypatch, original))

    assert len(run_state_service.calls) == 1
    assert run_state_service.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


@pytest.mark.asyncio
async def test_run_agent_propagates_settlement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落定自身失败（如数据库不可用）时异常上抛给执行器记录，且只尝试落定一次。"""

    run_state_service = _RecordingRunStateService(failure=RuntimeError("db down"))
    runtime = _make_runtime(monkeypatch, object(), run_state_service=run_state_service)

    with pytest.raises(RuntimeError, match="db down"):
        await runtime.run_agent(_make_agent(monkeypatch, RuntimeError("node exploded")))

    assert len(run_state_service.calls) == 1


@pytest.mark.asyncio
async def test_run_agent_settles_when_initial_delivery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初始投递在失败边界内：读库失败同样落定 failed，不留下没有执行者的 running Run。"""

    run_state_service = _RecordingRunStateService()
    runtime = _make_runtime(monkeypatch, object(), run_state_service=run_state_service)

    async def broken_delivery() -> None:
        raise RuntimeError("queue unavailable")

    await runtime.run_agent(_make_agent(monkeypatch, AssertionError("不应进入 workflow")),
                            broken_delivery)

    assert len(run_state_service.calls) == 1
    assert run_state_service.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


def test_status_changed_event_projects_controlled_error() -> None:
    """终态事件把受控错误写进 Run snapshot。"""

    state = _running_state()
    event = RunStatusChangedEvent(
        task_id=7,
        run_id=1,
        status=ConversationRunStatus.FAILED,
        end_reason=_QUOTA_CODE,
        error={
            "code": _QUOTA_CODE,
            "message": run_failure_message(_QUOTA_CODE),
        },
    )

    assert _error_mutation_values(state, event) == [
        {
            "code": _QUOTA_CODE,
            "message": run_failure_message(_QUOTA_CODE),
        }
    ]


def test_non_terminal_transition_clears_previous_error() -> None:
    """非终态迁移（如续跑）必须清空上一轮遗留的错误，避免旧失败文案粘住。"""

    state = _running_state()
    state["activity"]["error"] = {
        "code": _QUOTA_CODE,
        "message": run_failure_message(_QUOTA_CODE),
    }
    event = RunStatusChangedEvent(task_id=7, run_id=1, status=ConversationRunStatus.RUNNING)

    assert _error_mutation_values(state, event) == [None]


def test_build_run_error_projects_persisted_contract() -> None:
    """冷重建把持久化错误契约原样投影为 Transport 契约。"""

    run = SimpleNamespace(
        id=3,
        error={
            "code": _SERVICE_ERROR_CODE,
            "message": run_failure_message(_SERVICE_ERROR_CODE),
        },
    )

    assert ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run)) == {
        "code": _SERVICE_ERROR_CODE,
        "message": run_failure_message(_SERVICE_ERROR_CODE),
    }


def test_build_run_error_returns_none_without_persisted_error() -> None:
    """未失败或旧数据没有错误契约时投影为 ``None``。"""

    run = SimpleNamespace(id=3, error=None)

    assert ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run)) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED},
        {"code": "", "message": "运行失败"},
        {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED, "message": ""},
        {"code": 1, "message": "运行失败"},
        {
            "code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
            "message": "运行失败",
            "retryable": True,
        },
    ],
)
def test_build_run_error_rejects_malformed_payload(payload: dict[str, object]) -> None:
    """被污染的受控字段必须显式失败，不得投影成面向用户的文案。"""

    run = SimpleNamespace(id=3, error=payload)

    with pytest.raises(ValueError):
        ConversationTaskStateRebuilder.build_run_error(cast(ConversationRunRecord, run))


def test_validate_snapshot_rejects_malformed_run_error() -> None:
    """快照校验拒绝不合法的 Run 级错误契约。"""

    state = _running_state()
    state["activity"]["error"] = {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED}

    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_accepts_controlled_run_error() -> None:
    """合法错误契约通过校验，且 ``error`` 键是 Run 固定字段的一部分。"""

    state = _running_state()
    state["activity"]["error"] = {
        "code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
        "message": run_failure_message(Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED),
    }

    validate_snapshot(state)


@pytest.mark.asyncio
async def test_context_protocol_failure_has_local_code_and_preserves_diagnostic_boundary(
    monkeypatch,
):
    """上下文完整性失败落定为本地错误，不归因于模型服务、不把工具正文显示给用户。"""
    from app.core.context.context_entry import ContextEntry
    from app.core.context.tool_call_closure import ContextProtocolError

    error = ContextProtocolError(
        "orphan_tool_result",
        ContextEntry(
            {"role": "tool", "content": "private tool output", "tool_call_id": "ghost"},
            7,
            4,
        ),
        "ghost",
    )
    state = _RecordingRunStateService()
    runtime = _make_runtime(monkeypatch, object(), run_state_service=state)
    await runtime.run_agent(_make_agent(monkeypatch, error))
    assert len(state.calls) == 1
    assert state.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_CONTEXT_PROTOCOL_INVALID
    assert state.calls[0]["final_output"] == run_failure_message(
        Constant.Run.RUN_FAILURE_CODE_CONTEXT_PROTOCOL_INVALID
    )
    assert state.calls[0]["error_message"] is None
    assert "private tool output" not in str(state.calls)
