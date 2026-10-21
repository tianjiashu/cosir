"""节点 / 工作流测试用的 ``RuntimeContext`` 替身：字段名与生产类型一致，协作者是最小桩。

用例只覆盖它关心的字段（``runtime_stub(allows_tools=..., run_state=...)``）；其余字段给出不会
触发真实依赖的默认值。取消判定读 ``cancelled`` 标志，用例直接改它即可。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.core.workflow.execution_state.conversation_run_usage_stats import (
    ConversationRunUsageStats,
)
from app.core.workflow.execution_state.tool_batch import ToolBatch
from app.models.conversation_run_progress import ConversationRunProgress


def run_state_stub(**overrides: Any) -> SimpleNamespace:
    """Run 状态服务替身：迁移默认返回一个非 None 记录（视为命中）。"""
    stub = SimpleNamespace(
        transition=lambda *_args, **_kwargs: object(),
        save_execution=lambda *_args, **_kwargs: None,
        update_completed_result=lambda *_args, **_kwargs: None,
    )
    stub.__dict__.update(overrides)
    return stub


def runtime_stub(**overrides: Any) -> SimpleNamespace:
    rc = SimpleNamespace(
        run=SimpleNamespace(
            id=2, task_id=1, execution_data=ConversationRunProgress(), usage=None
        ),
        task=SimpleNamespace(id=1),
        workspace=SimpleNamespace(id=1, root_path="."),
        profile=SimpleNamespace(structured_output=None, max_steps=10),
        allows_tools=frozenset(),
        tool_definitions={},
        tool_executor=None,
        execution_context=None,
        run_state=run_state_stub(),
        inbox=SimpleNamespace(consume_into_context=lambda *_args: None),
        task_space=SimpleNamespace(
            task_tool_schemas=(),
            defer_runtime_notice=lambda _notice: None,
            take_runtime_notices=lambda **_kwargs: "",
        ),
        event_sink=lambda _event: None,
        user_input_decision_handlers=SimpleNamespace(handle_rejection=lambda *_args: None),
        model=None,
        thinking_channel="",
        step_limit=10,
        usage_stats=ConversationRunUsageStats(),
        batch=ToolBatch(),
        resume_decision=None,
        cancelled=False,
    )
    rc.__dict__.update(overrides)
    rc.is_cancelled = lambda: rc.cancelled
    return rc
