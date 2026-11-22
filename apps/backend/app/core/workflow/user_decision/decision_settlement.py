"""用户决定并入 context 时结算它回答的那次调用的等待占位。

这类工具没有执行结果，占位只能在并入的同一事务里收尾：推到 ``completed``（驳回也是——用户已经
作出决定），决定与载荷并进展示数据，卡片据此显示回答。占位离开 ``pending`` 后决定入口不再认它
在等待，重复提交被拒，「等你操作」的计数随状态事件更新。占位已不在等待（已作答、已作废）时不结算，
由调用方只确认、不写入这条决定。
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from app.core.events import ToolCallStatusChangedEvent
from app.core.workflow.message_queue.session_message import DecisionBody
from app.core.workflow.node_helper.common import _message_context, _runtime_context
from app.models.message_context.message_metadata.tool_message_metadata import ToolMessageMetadata


def settle_answered(
    body: DecisionBody, *, step_id: str, db: DbSession
) -> ToolCallStatusChangedEvent | None:
    """在并入决定的事务里结算占位，返回提交后才可发布的卡片事件；占位不在等待时返回 ``None``。"""
    rc = _runtime_context()
    context = _message_context()
    placeholder = context.tool_result_metadata(body.tool_call_id)
    if placeholder is None or not placeholder.awaiting_decision:
        return None
    written = context.update_tool_result_metadata(
        body.tool_call_id,
        ToolMessageMetadata(
            status="completed",
            display_data={
                **(placeholder.display_data or {}),
                "decision": body.decision,
                **(body.data or {}),
            },
        ),
        db=db,
    )
    return ToolCallStatusChangedEvent(
        session_id=rc.run.session_id,
        run_id=rc.run.id,
        step_id=step_id,
        tool_call_id=body.tool_call_id,
        result=written,
    )
