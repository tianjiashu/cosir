"""``TaskService.require_current_run``：区分「Task 不存在」与「Task 尚无 Run」。

调度入口（发送、重跑、actor 推进 / 取消 / 判定阶段）都靠它：新建的 Task 没有当前 Run 时返回
``None``，Task 不存在时 ``KeyError`` 上浮（接入层映射 404）。曾经按 ``current_run_id=None`` 去取
Run，得到 ``KeyError(None)``，新对话的第一次发送被误报为「task not found」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.service.depends import (
    get_conversation_run_crud,
    get_task_crud,
    get_task_service,
    get_workspace_crud,
)

pytestmark = pytest.mark.usefixtures("isolated_storage")


def _new_task() -> int:
    workspace = get_workspace_crud().create("current-run", str(Path.cwd()))
    return get_task_crud().create(workspace.id, "root").id


def test_task_without_any_run_has_no_current_run() -> None:
    task_id = _new_task()

    assert get_task_service().require_current_run(task_id) is None


def test_current_run_follows_the_task_pointer() -> None:
    task_id = _new_task()
    runs = get_conversation_run_crud()
    first = runs.create(task_id, status="completed")
    second = runs.create(task_id, status="running")
    # 当前 Run 以 Task 指针为准，不是「最新创建的那条」。
    get_task_crud().set_current_run_id(task_id, first.id)

    current = get_task_service().require_current_run(task_id)

    assert current is not None
    assert current.id == first.id != second.id


def test_missing_task_raises_key_error_instead_of_looking_like_no_run() -> None:
    with pytest.raises(KeyError):
        get_task_service().require_current_run(999_999)
