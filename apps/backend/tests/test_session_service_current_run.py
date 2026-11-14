"""``SessionService.require_current_run``：区分「Session 不存在」与「Session 尚无 Run」。

调度入口（发送、重跑、actor 推进 / 取消 / 判定阶段）都靠它：新建的 Session 没有当前 Run 时返回
``None``，Session 不存在时 ``KeyError`` 上浮（接入层映射 404）。曾经按 ``current_run_id=None`` 去取
Run，得到 ``KeyError(None)``，新会话的第一次发送被误报为「session not found」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.service.depends import (
    get_run_crud,
    get_session_crud,
    get_session_service,
    get_workspace_crud,
)

pytestmark = pytest.mark.usefixtures("isolated_storage")


def _new_session() -> int:
    workspace = get_workspace_crud().create("current-run", str(Path.cwd()))
    return get_session_crud().create(workspace.id, "root").id


def test_session_without_any_run_has_no_current_run() -> None:
    session_id = _new_session()

    assert get_session_service().require_current_run(session_id) is None


def test_current_run_follows_the_session_pointer() -> None:
    session_id = _new_session()
    runs = get_run_crud()
    first = runs.create(session_id, status="completed")
    second = runs.create(session_id, status="running")
    # 当前 Run 以 Session 指针为准，不是「最新创建的那条」。
    get_session_crud().set_current_run_id(session_id, first.id)

    current = get_session_service().require_current_run(session_id)

    assert current is not None
    assert current.id == first.id != second.id


def test_missing_session_raises_key_error_instead_of_looking_like_no_run() -> None:
    with pytest.raises(KeyError):
        get_session_service().require_current_run(999_999)
