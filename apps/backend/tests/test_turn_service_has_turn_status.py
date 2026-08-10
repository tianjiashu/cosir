"""``TurnService.has_turn_status`` 空值保护单测。

聚焦验证修复点：当 turn 不存在时，``has_turn_status`` 必须返回 ``False``
而非抛出 ``KeyError``（``turn_crud.get`` 在缺失时抛 ``KeyError``），否则取消
检测会失效，导致已取消的 turn 仍进入工具执行。

不依赖真实数据库装配（避免跨库 FK / session 不一致），直接 stub ``TurnCrud``
验证 ``has_turn_status`` 的空值保护分支与正常匹配分支。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.service.task.turn_service import TurnService


class _StubTurnCrud:
    """最小 turn CRUD stub：不存在的 turn 抛 KeyError，存在的按 status 返回。"""

    def __init__(self, records: dict[str, str]) -> None:
        self._records = records

    def get(self, turn_id: str):
        if turn_id not in self._records:
            raise KeyError(turn_id)
        return SimpleNamespace(turn_id=turn_id, status=self._records[turn_id])


def _build_service(records: dict[str, str]) -> TurnService:
    """构造仅注入 stub turn_crud 的 TurnService。"""

    service = object.__new__(TurnService)
    service._turn = _StubTurnCrud(records)
    return service


def test_missing_turn_returns_false_not_keyerror() -> None:
    """turn 不存在时返回 False，不抛 KeyError（修复回归点）。"""

    service = _build_service({"turn-a": "cancelled"})
    assert service.has_turn_status("turn-missing", "cancelled") is False


def test_null_turn_id_returns_false_without_raising() -> None:
    """turn_id 为 None 时返回 False 且不抛（守卫非法输入，不吞真实缺陷）。"""

    service = _build_service({"turn-a": "cancelled"})
    assert service.has_turn_status(None, "cancelled") is False


def test_existing_cancelled_turn_matches_status() -> None:
    """已存在的 cancelled turn 应正确匹配 cancelled 状态。"""

    service = _build_service({"turn-a": "cancelled"})
    assert service.has_turn_status("turn-a", "cancelled") is True
    assert service.has_turn_status("turn-a", "running") is False
