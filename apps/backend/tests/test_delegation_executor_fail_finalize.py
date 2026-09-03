"""``DelegationExecutor._fail_delegation`` 收口逻辑的单元测试。

验证「acquire 成功后所有提前退出路径都确定性终态化」这一并发额度防泄漏契约：
- 正常路径：delegation 被推进到 failed。
- mark_failed 自身抛异常时：被吞掉且不二次抛出（避免掩盖根因），delegation_id
  为 None 时直接跳过。
"""

from datetime import datetime
from unittest.mock import MagicMock

from app.core.agents.agent_profile import AgentProfile
from app.core.delegation.delegation_executor import DelegationExecutor
from app.models import ConversationRunRecord, TaskRecord


def _make_executor() -> DelegationExecutor:
    """构造最小可用的 ``DelegationExecutor`` 桩（仅用于测试 ``_fail_delegation``）。

    返回:
        依赖全部以占位对象填充的执行器实例；``child_runner`` 等协作者不参与本测试。
    """

    parent_turn = ConversationRunRecord(
        id=1,
        task_id=1,
        input_text="parent",
        status="running",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    parent_task = TaskRecord(
        id=1,
        workspace_id="ws-1",
        title="parent task",
        execution_status="running",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    parent_profile = AgentProfile(
        agent_id="developer",
        role="developer",
        description="developer profile",
        allowed_tools=[],
    )
    return DelegationExecutor(
        child_runner=MagicMock(),
        parent_profile=parent_profile,
        parent_run=parent_turn,
        parent_task=parent_task,
    )


def test_fail_delegation_marks_failed() -> None:
    """delegation_id 非空时，``_fail_delegation`` 调用 ``mark_failed`` 终态化。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    executor._fail_delegation(
        "deleg-1",
        delegation_service,
        None,
        "boom",
    )

    delegation_service.mark_failed.assert_called_once()
    args, _kwargs = delegation_service.mark_failed.call_args
    assert args[0] == "deleg-1"
    assert args[1] == "boom"


def test_fail_delegation_skips_when_none() -> None:
    """delegation_id 为 None（acquire 失败）时直接跳过，不调用 ``mark_failed``。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    executor._fail_delegation(
        None,
        delegation_service,
        None,
        "boom",
    )

    delegation_service.mark_failed.assert_not_called()


def test_fail_delegation_swallows_mark_failed_error() -> None:
    """``mark_failed`` 自身抛异常时被吞掉且不二次抛出，避免掩盖根因。"""

    executor = _make_executor()
    delegation_service = MagicMock()
    delegation_service.mark_failed.side_effect = RuntimeError("db down")

    # 不应抛出
    executor._fail_delegation(
        "deleg-1",
        delegation_service,
        None,
        "boom",
    )

    delegation_service.mark_failed.assert_called_once()
