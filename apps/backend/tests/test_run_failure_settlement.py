"""异常逃逸时的 Run 终态收口契约测试。

本模块锁定本次修复的核心不变量：**任何从 graph 逃逸的异常都必须先把 Run 落定为 failed
再向上抛出**。历史缺陷是 workflow / runner / executor 三方都认为「终态由对方落定」，实际
无人落定，导致 provider 报错后 Run 永久停留在 ``running``、前端既不显示失败原因也无法开始
新轮次。

同时覆盖配套的受控错误投影：终态事件携带 ``error``、冷重建从持久化契约投影 ``error``、
快照校验拒绝被污染的 ``error``。
"""

from __future__ import annotations

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
from app.core.workflows.react.workflow import ReactLikeWorkflow
from app.core.workflows.workflow_operations import WorkflowOperations
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.models.enums.error_kind import ErrorKind

_QUOTA_CODE = ErrorKind.MODEL_INSUFFICIENT_QUOTA.value
_SERVICE_ERROR_CODE = ErrorKind.MODEL_SERVICE_ERROR.value


class _StatusError(Exception):
    """等价于 provider SDK 状态异常的替身：只带 ``status_code``。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


class _RecordingOperations:
    """只实现 ``_settle_failed_run`` 依赖入口的假操作门面。

    记录每次 ``fail_run_if_running`` 的入参，并按需模拟「落定失败」与「终态竞态落空」。
    """

    def __init__(self, *, settles: bool = True, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._settles = settles
        self._failure = failure

    def get_current_run(self) -> SimpleNamespace:
        """返回当前 Run 的最小替身（收口逻辑只读取 ``id``）。"""

        return SimpleNamespace(id=7)

    def fail_run_if_running(
        self,
        *,
        end_reason: str | None = None,
        usage_stats: object = None,
        final_output: str | None = None,
    ) -> SimpleNamespace | None:
        """记录落定入参；``failure`` 非空时抛出，``settles`` 为假时返回 None。"""

        self.calls.append(
            {
                "end_reason": end_reason,
                "usage_stats": usage_stats,
                "final_output": final_output,
            }
        )
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(id=7) if self._settles else None


def _running_state() -> ConversationStateSnapshot:
    """构造只含一个 running Run 的合法 snapshot（含新的 ``error`` 键）。"""

    state = empty_snapshot()
    state["runs"] = [
        ConversationRunSnapshot(
            runId=1,
            status="running",
            endReason=None,
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


def test_settle_failed_run_writes_catalog_message_as_final_output() -> None:
    """默认 ``final_output`` 取失败 code 对应的受控文案，避免把异常原文写进终态。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    workflow._settle_failed_run(
        cast(WorkflowOperations, operations), _QUOTA_CODE
    )

    assert operations.calls == [
        {
            "end_reason": _QUOTA_CODE,
            "usage_stats": None,
            "final_output": run_failure_message(_QUOTA_CODE),
        }
    ]


def test_settle_failed_run_honours_explicit_final_output_and_usage() -> None:
    """调用方给出 ``final_output`` 与用量时按原样透传。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()
    usage = SimpleNamespace(total_tokens=12)

    workflow._settle_failed_run(
        cast(WorkflowOperations, operations),
        Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
        usage_stats=cast(object, usage),
        final_output="自定义说明",
    )

    assert operations.calls == [
        {
            "end_reason": Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED,
            "usage_stats": usage,
            "final_output": "自定义说明",
        }
    ]


def test_settle_failed_run_tolerates_lost_terminal_race() -> None:
    """Run 已被其它路径落终态（返回 None）时正常返回，不抛异常。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(settles=False)

    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)

    assert len(operations.calls) == 1


def test_settle_failed_run_never_masks_original_failure() -> None:
    """落终态自身失败（如数据库不可用）时只记日志，不向调用方抛出新异常。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations(failure=RuntimeError("database unavailable"))

    workflow._settle_failed_run(cast(WorkflowOperations, operations), Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED)

    assert len(operations.calls) == 1


@pytest.mark.asyncio
async def test_run_settles_failed_run_before_propagating_graph_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """graph 抛 provider 异常时，Run 必须先落 failed 终态再向上抛出。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise _StatusError(402, "Insufficient Balance")

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(_StatusError):
        await workflow.run(cast(WorkflowOperations, operations))

    assert operations.calls == [
        {
            "end_reason": _QUOTA_CODE,
            "usage_stats": None,
            "final_output": run_failure_message(_QUOTA_CODE),
        }
    ]


@pytest.mark.asyncio
async def test_run_classifies_server_side_failure_as_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx 类异常归类为模型服务不可用，而不是笼统的图执行失败。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise _StatusError(503, "upstream unavailable")

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(_StatusError):
        await workflow.run(cast(WorkflowOperations, operations))

    assert operations.calls[0]["end_reason"] == _SERVICE_ERROR_CODE


@pytest.mark.asyncio
async def test_run_falls_back_to_graph_failed_for_unclassifiable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无法归类为模型错误的异常使用中性兜底 code，而不是猜测成模型问题。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()

    async def _boom(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("node exploded")

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _boom)

    with pytest.raises(RuntimeError):
        await workflow.run(cast(WorkflowOperations, operations))

    assert operations.calls[0]["end_reason"] == Constant.Run.RUN_FAILURE_CODE_GRAPH_FAILED


@pytest.mark.asyncio
async def test_run_leaves_terminal_settlement_to_nodes_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常结束路径不得重复落定终态（终态由节点负责）。"""

    workflow = ReactLikeWorkflow()
    operations = _RecordingOperations()
    invocations: list[int] = []

    async def _ok(_self: ReactLikeWorkflow, *_args: object, **_kwargs: object) -> None:
        invocations.append(1)

    monkeypatch.setattr(ReactLikeWorkflow, "_run_graph", _ok)

    await workflow.run(cast(WorkflowOperations, operations))

    assert invocations == [1]
    assert operations.calls == []


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
