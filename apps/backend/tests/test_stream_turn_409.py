"""针对 stream_turn 409 守卫的 API 行为测试（重点新增）。

验证改动点：``GET /turns/{turn_id}/stream`` 对非 pending 轮次直接返回 409，
不再回放历史事件。同时确认「pending 才进入 SSE」由该守卫保证。

导入前置：仓库存在坏导入链，需在导入 app 模块之前注入 app.storage.crud.log 占位。
"""

import sys

sys.path.insert(0, ".")

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

import pytest
from fastapi import HTTPException

from app.api import turns_api
from app.models import TurnRecord
from app.models.enums.event_type import EventType
from app.models.runtime_event import RuntimeEvent


class FakeRuntime:
    """记录 run_turn 是否真的被调用（守卫未命中时会触发）。"""

    def __init__(self) -> None:
        self.run_turn_called = False

    async def run_turn(self, turn_id, turn=None):
        self.run_turn_called = True
        yield RuntimeEvent(
            event_type=EventType.RUN_STARTED, task_id="task1", turn_id=turn_id, payload={}
        )


class FakeTurnServiceCompleted:
    """get_turn 返回已完成轮次（非 pending）。"""

    def __init__(self) -> None:
        self.got = None

    def get_turn(self, turn_id: str) -> TurnRecord:
        self.got = turn_id
        return TurnRecord(
            turn_id=turn_id,
            task_id="task1",
            input_text="hi",
            status="completed",
            created_at=None,
            updated_at=None,
            end_reason=None,
            response_text="done",
        )


class FakeTurnServiceMissing:
    """get_turn 抛 KeyError（轮次不存在）。"""

    def get_turn(self, turn_id: str) -> TurnRecord:
        raise KeyError(turn_id)


# 测试目的：非 pending 轮次触发 409 守卫，且不调用 run_turn（避免回放/误执行）。缺陷：守卫缺失导致回放历史或执行非 pending。
async def test_stream_turn_non_pending_returns_409():
    runtime = FakeRuntime()
    svc = FakeTurnServiceCompleted()
    with pytest.raises(HTTPException) as exc:
        # 直接调用路由函数，传入 Fake 依赖，绕过 app 装配与 lifespan。
        await turns_api.stream_turn("t1", runtime=runtime, turn_service=svc)
    assert exc.value.status_code == 409
    assert "not pending" in exc.value.detail
    assert runtime.run_turn_called is False
    assert svc.got == "t1"


# 测试目的：pending 轮次应越过守卫、进入 SSE 并调用 run_turn。缺陷：守卫误杀 pending 轮次。
async def test_stream_turn_pending_enters_sse():
    runtime = FakeRuntime()
    svc = FakeTurnServiceCompleted()

    # 用一个始终 pending 的 service 覆盖 get_turn。
    class PendingSvc(FakeTurnServiceCompleted):
        def get_turn(self, turn_id: str) -> TurnRecord:
            rec = super().get_turn(turn_id)
            object.__setattr__(rec, "status", "pending")
            return rec

    pending_svc = PendingSvc()
    resp = await turns_api.stream_turn("t1", runtime=runtime, turn_service=pending_svc)
    assert resp is not None  # StreamingResponse 已构造（pending 路径）
    # 守卫已放行；run_turn 仅在消费 StreamingResponse 时惰性触发，此处尚未消费。
    assert pending_svc.got == "t1"


# 测试目的：turn 不存在应 404，而非 409/500。缺陷：错误码错用导致客户端误判。
async def test_stream_turn_missing_returns_404():
    runtime = FakeRuntime()
    svc = FakeTurnServiceMissing()
    with pytest.raises(HTTPException) as exc:
        await turns_api.stream_turn("ghost", runtime=runtime, turn_service=svc)
    assert exc.value.status_code == 404
    assert "turn not found" in exc.value.detail


# 测试目的：409 守卫的 detail 精确指向历史接口（GET /tasks/{task_id}/turns）。缺陷：回退文案误导客户端重连流。
async def test_stream_turn_409_detail_points_to_history_endpoint():
    runtime = FakeRuntime()
    svc = FakeTurnServiceCompleted()
    with pytest.raises(HTTPException) as exc:
        await turns_api.stream_turn("t1", runtime=runtime, turn_service=svc)
    assert exc.value.status_code == 409
    assert "/tasks/{task_id}/turns" in exc.value.detail


# ---------------------------------------------------------------------------
# create_turn / cancel_turn 端点守卫
# ---------------------------------------------------------------------------


class _FakeTaskServiceMissing:
    """create_turn 抛 KeyError（task 不存在）。"""

    def create_turn(self, task_id, input_text):
        raise KeyError(task_id)


class _FakeTaskServiceOk:
    """create_turn 返回带 turn_id 的记录（已弃用，保留占位避免误引用）。"""


# 测试目的：create_turn 在 task 不存在时返回 404。缺陷：错误码误用导致客户端误判。
async def test_create_turn_missing_task_returns_404():
    req = type("Req", (), {"input_text": "hi"})()
    with pytest.raises(HTTPException) as exc:
        await turns_api.create_turn("ghost-task", req, turn_service=_FakeTaskServiceMissing())
    assert exc.value.status_code == 404
    assert "task not found" in exc.value.detail


# 测试目的：create_turn 在 service 抛 ValueError（非法输入）时返回 400。缺陷：非法输入被入库或错误码错用。
async def test_create_turn_invalid_input_returns_400():
    class _BadSvc:
        def create_turn(self, task_id, input_text):
            raise ValueError("input_text must be a non-empty string")

    req = type("Req", (), {"input_text": "   "})()
    with pytest.raises(HTTPException) as exc:
        await turns_api.create_turn("task1", req, turn_service=_BadSvc())
    assert exc.value.status_code == 400


# 测试目的：create_turn 正常输入返回 200 且 to_dict 含 turn_id。缺陷：成功路径漏返回字段。
async def test_create_turn_ok_returns_dict():
    from datetime import datetime, timezone

    req = type("Req", (), {"input_text": "hello"})()

    class _OkSvc:
        def create_turn(self, task_id, input_text):
            from app.models import TurnRecord

            now = datetime.now(timezone.utc)
            return TurnRecord(
                turn_id="new-turn",
                task_id=task_id,
                input_text=input_text,
                status="pending",
                created_at=now,
                updated_at=now,
            )

    result = await turns_api.create_turn("task1", req, turn_service=_OkSvc())
    assert result["turn_id"] == "new-turn"
    assert result["status"] == "pending"


# 测试目的：cancel_turn 在 turn 不存在时返回 404。缺陷：错误码误用。
async def test_cancel_turn_missing_returns_404():
    class _Svc:
        def get_turn(self, turn_id):
            raise KeyError(turn_id)

    with pytest.raises(HTTPException) as exc:
        await turns_api.cancel_turn("ghost", runtime=FakeRuntime(), turn_service=_Svc())
    assert exc.value.status_code == 404
    assert "turn not found" in exc.value.detail


# 测试目的：cancel_turn 正常路径返回被取消 turn 的 to_dict，status=cancelled。缺陷：成功路径漏返回/状态错。
async def test_cancel_turn_ok_returns_cancelled():
    from datetime import datetime, timezone
    from app.models import TurnRecord
    from types import SimpleNamespace

    canceled = TurnRecord(
        turn_id="t1",
        task_id="task1",
        input_text="hi",
        status="cancelled",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        end_reason="user_cancelled",
    )

    class _Svc:
        def get_turn(self, turn_id):
            return canceled

    fake_runtime = SimpleNamespace(cancel_turn=lambda turn_id: canceled)
    result = await turns_api.cancel_turn("t1", runtime=fake_runtime, turn_service=_Svc())
    assert result["turn_id"] == "t1"
    assert result["status"] == "cancelled"
    assert result["end_reason"] == "user_cancelled"
