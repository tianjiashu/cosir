"""派生深度：规则表的增量、Session 记录往返与配置项登记。

按深度给不给派生工具见 ``test_session_tool_scope``。
"""

from types import SimpleNamespace

import pytest

from app.config.environment_field_catalog import ENVIRONMENT_FIELDS
from app.core.tool.multi_agent.derivation import DERIVATIONS, child_depth
from app.models.session_extra import SessionExtra
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from tests.session_extra_fixtures import fork_extra


def test_derivations_go_one_deeper() -> None:
    parent = SimpleNamespace(derivation_depth=1)
    assert child_depth(parent, "delegate") == 2  # type: ignore[arg-type]
    assert child_depth(parent, "fork") == 2  # type: ignore[arg-type]
    assert {kind: (rule.tool, rule.creates) for kind, rule in DERIVATIONS.items()} == {
        "delegate": ("delegate_task", "delegated"),
        "fork": ("fork_session", "agent_fork"),
    }


@pytest.mark.usefixtures("isolated_storage")
def test_depth_round_trips_and_defaults_to_zero(tmp_path) -> None:
    workspace = WorkspaceCrud().create("depth", str(tmp_path))
    crud = SessionCrud()
    root = crud.create(workspace.id, "root")
    child = crud.create(
        workspace.id, "child", session_type="delegated", parent_session_id=root.id,
        derivation_depth=2,
    )

    assert crud.get(root.id).derivation_depth == 0
    assert crud.get(child.id).derivation_depth == 2


def test_depth_limit_is_configurable_with_bounds() -> None:
    field = ENVIRONMENT_FIELDS["AGENT_MAX_DERIVATION_DEPTH"]
    assert (field.group_id, field.default, field.minimum, field.maximum) == ("general", 2, 1, 5)


def test_only_sessions_this_agent_derived_answer_to_it() -> None:
    """``send_message`` / ``status`` 只能寻址自己委派或 Agent fork 出的 Session。

    不能寻址用户手动 fork 的对话。
    """
    from datetime import UTC, datetime

    from app.models.session_record import SessionRecord

    now = datetime.now(UTC)

    def session(session_type: str, *, parent: int | None = None, fork: SessionExtra | None = None) -> SessionRecord:
        return SessionRecord(
            id=9, workspace_id=1, title="t", created_at=now, updated_at=now,
            session_type=session_type, parent_session_id=parent, extra=fork or SessionExtra(),
        )

    child = session("delegated", parent=1)
    agent_fork = session("agent_fork", fork=fork_extra(1))
    user_fork = session("user_fork", fork=fork_extra(1))
    assert child.derived_by(1) and not child.derived_by(2)
    assert agent_fork.derived_by(1) and not agent_fork.derived_by(2)
    assert not user_fork.derived_by(1)
