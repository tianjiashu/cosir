"""针对 TurnRecord.response_text 落库链路的轻量单元验证。

覆盖：``TurnRecord.to_dict`` 携带 ``response_text``；``TurnService.update_turn_response``
正确委托到 ``TurnCrud``。DB 集成（列迁移 + UPDATE）由独立测试 Agent 负责。

导入前置：仓库存在坏导入链，需在导入 app 模块之前注入 app.storage.crud.log 占位。
"""

import sys
from datetime import datetime

sys.path.insert(0, ".")

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

from app.models import TurnRecord
from app.service.task.turn_service import TurnService


def test_turn_record_to_dict_includes_response_text():
    """to_dict 必须包含 response_text，历史接口才能直接返回 Agent 回复。"""

    rec = TurnRecord(
        turn_id="t1",
        task_id="task1",
        input_text="hi",
        status="completed",
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
        end_reason=None,
        response_text="hello",
    )
    assert rec.to_dict()["response_text"] == "hello"


class _FakeTurnCrud:
    """记录 update_response 调用，不触碰数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def update_response(self, turn_id: str, response_text: str | None) -> None:
        self.calls.append((turn_id, response_text))
        return None


def test_turn_service_update_turn_response_delegates():
    """TurnService.update_turn_response 应原样委托给底层 TurnCrud。"""

    svc = TurnService.__new__(TurnService)
    svc._turn = _FakeTurnCrud()
    svc.update_turn_response("t1", "answer")
    assert svc._turn.calls == [("t1", "answer")]
