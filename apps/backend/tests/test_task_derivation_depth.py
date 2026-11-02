"""派生深度：规则表的增量、Task 记录往返与配置项登记。入口检查见委派与 Team 工具的契约测试。"""

from types import SimpleNamespace

import pytest

from app.config.environment_field_catalog import ENVIRONMENT_FIELDS
from app.config.settings import Settings
from app.core.tool.derivation import check_derivation_depth
from app.models.task_kind import DERIVATIONS, child_depth
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud


def test_team_nodes_share_the_main_task_depth_and_other_derivations_go_one_deeper() -> None:
    parent = SimpleNamespace(derivation_depth=1)
    assert child_depth(parent, "delegate") == 2  # type: ignore[arg-type]
    assert child_depth(parent, "fork") == 2  # type: ignore[arg-type]
    assert child_depth(parent, "team") == 1  # type: ignore[arg-type]
    assert {kind: rule.creates for kind, rule in DERIVATIONS.items()} == {
        "delegate": "delegate_task",
        "fork": "fork",
        "team": "agent_team_node",
    }


def test_depth_check_reads_the_current_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """上限每次现读：调小后已经超过它的 Task 同样不能再派生。"""
    parent = SimpleNamespace(id=1, derivation_depth=1)
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 2)
    assert check_derivation_depth("delegate_task", parent) is None  # type: ignore[arg-type]
    monkeypatch.setattr(Settings, "AGENT_MAX_DERIVATION_DEPTH", 1)
    refused = check_derivation_depth("delegate_task", parent)  # type: ignore[arg-type]
    assert refused is not None and refused.status == "error"
    assert "maximum derivation depth is 1" in refused.content


@pytest.mark.usefixtures("isolated_storage")
def test_depth_round_trips_and_defaults_to_zero(tmp_path) -> None:
    workspace = WorkspaceCrud().create("depth", str(tmp_path))
    crud = TaskCrud()
    root = crud.create(workspace.id, "root")
    child = crud.create(
        workspace.id, "child", task_type="delegate_task", parent_task_id=root.id,
        derivation_depth=2,
    )

    assert crud.get(root.id).derivation_depth == 0
    assert crud.get(child.id).derivation_depth == 2


def test_depth_limit_is_configurable_with_bounds() -> None:
    field = ENVIRONMENT_FIELDS["AGENT_MAX_DERIVATION_DEPTH"]
    assert (field.group_id, field.default, field.minimum, field.maximum) == ("general", 2, 1, 5)


def test_only_tasks_this_agent_derived_answer_to_it() -> None:
    """``child_agent_send`` / ``status`` 只能寻址自己委派或 Agent fork 出的 Task：不能绕过 Coordinator
    找节点，也不能寻址用户手动 fork 的对话。"""
    from datetime import UTC, datetime

    from app.models.task_record import TaskRecord

    now = datetime.now(UTC)

    def task(task_type: str, *, parent: int | None = None, fork: dict | None = None) -> TaskRecord:
        return TaskRecord(
            id=9, workspace_id=1, title="t", created_at=now, updated_at=now,
            task_type=task_type, parent_task_id=parent, extra={"fork": fork} if fork else None,
        )

    child = task("delegate_task", parent=1)
    agent_fork = task("fork", fork={"source_task_id": 1, "created_by": "agent"})
    user_fork = task("fork", fork={"source_task_id": 1, "source_message_id": "3"})
    node = task("agent_team_node", parent=1)
    assert child.derived_by(1) and not child.derived_by(2)
    assert agent_fork.derived_by(1) and not agent_fork.derived_by(2)
    assert not user_fork.derived_by(1)
    assert not node.derived_by(1)
