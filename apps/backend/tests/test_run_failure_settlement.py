"""异常逃逸时的 Run 终态收口契约测试。

本模块锁定核心不变量：**任何从 workflow 逃逸的异常都必须由 ``AgentRuntime.run_agent`` 的
异常边界当场把 Run 落定为 failed**。历史缺陷是 workflow / runner / executor 三方都认为
「终态由对方落定」，实际无人落定，导致 provider 报错后 Run 永久停留在 ``running``、前端既
不显示失败原因也无法开始新轮次。

收口细节（当前实现）：

- 落定直接走 run 级状态服务（``fail_run_if_running``，条件更新），**不依赖** ``WorkflowOperations``
  门面是否构造成功——门面构建失败时同样能落定，正是这一点覆盖了「workflow 还没进入就失败」；
- 分类失败 code、写受控文案、把模型响应体 ``message`` 作为受控 UI 消息一并落库；
- 落定完成后异常**不再上抛**：由执行器收尾兜底处理仍未落终态的 run（落定失败时异常才继续
  上抛，因为那条路径需要执行器的兜底收敛）。

同时覆盖配套的受控错误投影：终态事件携带 ``error``、冷重建从持久化契约投影 ``error``、
快照校验拒绝被污染的 ``error``。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest

from app.assistant_transport.event import RunStatusChangedEvent
from app.assistant_transport.service.conversation_task_state_rebuilder import (
    ConversationTaskStateRebuilder,
)
from app.assistant_transport.state.conversation_run_snapshot import ConversationRunSnapshot
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.config.constant import Constant
from app.core.runtime import runner as runner_module
from app.core.runtime.runner import AgentRuntime
from app.core.workflows.agent_workflow import WorkflowRunFailure
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.enums.error_kind import ErrorKind

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
    """记录 ``fail_run_if_running`` 入参的假 run 级状态服务。

    覆盖三种落定结果：正常落定、条件更新落空（Run 已被别处落终态）、落定自身失败。
    """

    def __init__(self, *, settles: bool = True, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._settles = settles
        self._failure = failure

    def fail_run_if_running(
        self,
        run_id: int,
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
        return SimpleNamespace(id=run_id) if self._settles else None


def _running_state() -> ConversationStateSnapshot:
    """构造只含一个 running Run 的合法 snapshot（含新的 ``error`` 键）。"""

    state = empty_snapshot()
    state["runs"] = [
        ConversationRunSnapshot(
            runId=1,
            status="running",
            endReason=None,
            langfuseTraceId=None,
            messages=[],
            usage=None,
            error=None,
        )
    ]
    state["current_run_id"] = 1
    return state


def _error_mutation_values(
    state: ConversationStateSnapshot, event: RunStatusChangedEvent
) -> list[object]:
    """取出事件 plan 中对 Run ``error`` 写入的全部取值。"""

    mutations = event.plan(state)
    return [mutation.value for mutation in mutations if mutation.path == ("runs", 0, "error")]


def test_failure_code_for_classifies_timeout() -> None:
    """超时异常归类为模型超时码。"""

    assert AgentRuntime._failure_code_for(_TimeoutFailure()) == ErrorKind.MODEL_TIMEOUT.value


def test_failure_code_for_classifies_connection_error() -> None:
    """连接失败归类为网络错误，而不是笼统的图执行失败。"""

    assert AgentRuntime._failure_code_for(_ConnectFailure()) == ErrorKind.MODEL_NETWORK_ERROR.value


def test_failure_code_for_falls_back_to_graph_failed() -> None:
    """无法归类为模型错误的异常使用中性兜底 code，而不是猜测成模型问题。"""

    assert (
        AgentRuntime._failure_code_for(RuntimeError("node exploded"))
        == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
    )


def test_failure_code_for_honours_workflow_failure_code() -> None:
    """工作流已判定失败语义时直接采用其自带 code，不做二次猜测。"""

    failure = WorkflowRunFailure(
        Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE, "运行期模型解析失败"
    )

    assert (
        AgentRuntime._failure_code_for(failure)
        == Constant.Run.RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE
    )


def _make_agent(workflow_error: BaseException) -> SimpleNamespace:
    """构造带失败 workflow 的 agent profile 替身（``run`` 字段必须非 None）。"""

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise workflow_error

    return SimpleNamespace(
        agent_id="main_agent",
        run=SimpleNamespace(id=7, task_id=3),
        workflow=SimpleNamespace(run=_boom),
    )


def _make_runtime(
    monkeypatch: pytest.MonkeyPatch,
    operations: object | None,
    *,
    build_operations_error: BaseException | None = None,
    run_state_service: _RecordingRunStateService | None = None,
) -> AgentRuntime:
    """构造只保留 ``run_agent`` 异常边界所需协作者的 AgentRuntime 替身。

    真实 service 装配、hook 触发与观测上下文全部替换为最小替身，使用例只观察
    「workflow 异常 → 分类 → 落定」这条链路；``build_operations_error`` 非空时模拟门面尚未
    构造就失败的路径。落定入口（run 级状态服务）在 ``__init__`` 中捕获，故这里必须显式注入。
    """

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._task_service = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(id=task_id, workspace_id=9)
    )
    runtime._workspace_service = SimpleNamespace(
        get_workspace=lambda workspace_id: SimpleNamespace(id=workspace_id)
    )
    runtime._conversation_run_state_service = run_state_service or _RecordingRunStateService()

    async def _noop_fire(_context: object) -> None:
        return None

    @asynccontextmanager
    async def _trace(_metadata: object) -> object:
        yield SimpleNamespace(trace_id=None, callbacks=[], tool_trace_recorder=None)

    def _build(_self: AgentRuntime, *_args: object, **_kwargs: object) -> object:
        if build_operations_error is not None:
            raise build_operations_error
        return operations

    monkeypatch.setattr(runner_module.HookInterceptor, "async_safe_fire", _noop_fire)
    monkeypatch.setattr(runner_module, "conversation_run_trace", _trace)
    monkeypatch.setattr(AgentRuntime, "_build_operations", _build)
    return runtime


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

    await runtime.run_agent(_make_agent(original))

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

    await runtime.run_agent(_make_agent(RuntimeError("node exploded")))

    assert len(run_state_service.calls) == 1


@pytest.mark.asyncio
async def test_run_agent_settles_even_when_operations_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """门面尚未构造就失败时仍要落定：落定走 run 级状态服务，不依赖门面。

    这条路径正是「workflow 还没进入就失败」的覆盖点，若不落定就会留下僵尸 running，
    执行器的兜底收敛还会把失败伪装成 cancelled。
    """

    run_state_service = _RecordingRunStateService()
    original = RuntimeError("operations unavailable")
    runtime = _make_runtime(
        monkeypatch, None, build_operations_error=original, run_state_service=run_state_service
    )

    await runtime.run_agent(_make_agent(original))

    assert len(run_state_service.calls) == 1
    assert (
        run_state_service.calls[0]["end_reason"]
        == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED
    )


@pytest.mark.asyncio
async def test_run_agent_propagates_settlement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落定自身失败（如数据库不可用）时异常上抛，由执行器兜底收敛且只落定一次。

    这条路径不能吞掉：Run 仍是 running，必须让执行器的 ``_converge_unfinished_run`` 接手。
    """

    run_state_service = _RecordingRunStateService(failure=RuntimeError("db down"))
    runtime = _make_runtime(monkeypatch, object(), run_state_service=run_state_service)

    with pytest.raises(RuntimeError, match="db down"):
        await runtime.run_agent(_make_agent(RuntimeError("node exploded")))

    assert len(run_state_service.calls) == 1


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
    state["runs"][0]["error"] = {
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

    assert ConversationTaskStateRebuilder.build_run_error(
        cast(ConversationRunRecord, run)
    ) == {
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
        {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED, "message": "运行失败", "retryable": True},
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
    state["runs"][0]["error"] = {"code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED}

    with pytest.raises(ValueError):
        validate_snapshot(state)


def test_validate_snapshot_accepts_controlled_run_error() -> None:
    """合法错误契约通过校验，且 ``error`` 键是 Run 固定字段的一部分。"""

    state = _running_state()
    state["runs"][0]["error"] = {
        "code": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
        "message": run_failure_message(Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED),
    }

    validate_snapshot(state)
