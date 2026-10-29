"""Run 之外结算延迟结果的后台占位行：撤销、用户取消与重启（设计文档「卡片状态」）。

占位行是卡片状态的事实源：后台占位以 ``pending``（``background``）写入，结论并入时由 workflow 在 Run
内推到 ``completed``（见 ``tool_observation_projection.settle_deferred_placeholder``）。这里处理不在
Run 内发生的结算：写库后经 projector 投影，但只投影到已物化的 snapshot——重启收尾会扫过全部 Task，
不能为每个 Task 物化 space 与 actor；未物化的 snapshot 下次重建自会读到占位行的新状态。投影失败只
记录，理由相同。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.service import depends as service_depends

# 卡片短提示（约 5 字）：区分「被谁结束」，状态本身都是 ``cancelled``。
USER_CANCELLED_HINT = "已取消"
RUN_STOPPED_HINT = "已停止"
INTERRUPTED_HINT = "已中断"


def settle_placeholders(
    task_id: int | None,
    tool_call_ids: Iterable[str] | None,
    status: ToolCallEventStatus,
    hint: str | None,
) -> int:
    """把这些调用仍为 ``pending`` 的后台占位推到 ``status``，返回改写的行数。

    ``task_id`` 为 ``None`` 时跨全部 Task（重启）；``tool_call_ids`` 为 ``None`` 时不按调用过滤。
    同步写库，调用方放在工作线程。
    """

    ids = None if tool_call_ids is None else frozenset(tool_call_ids)
    if ids is not None and not ids:
        return 0
    settled = service_depends.get_conversation_task_context_service().settle_deferred_placeholders(
        task_id, ids, status, hint
    )
    from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

    projector = service_depends.get_conversation_event_projector()
    for record in settled:
        tool_call_id = str(record.message.get("tool_call_id"))
        space = task_runtime_spaces.get(record.task_id)
        if space is None or not space.has_snapshot():
            continue
        try:
            projector.process(
                ToolCallStatusChangedEvent(
                    task_id=record.task_id,
                    run_id=record.run_id,
                    tool_call_id=tool_call_id,
                    status=status,
                    error=hint,
                )
            )
        except (KeyError, ValueError):
            log.warning(
                "deferred_placeholder_projection_skipped",
                extra={
                    "msg": "后台占位已结算，snapshot 投影跳过（下次重建读到）",
                    "data": {"task_id": record.task_id, "tool_call_id": tool_call_id},
                },
                exc_info=True,
            )
    if settled:
        log.info(
            "deferred_placeholders_settled",
            extra={
                "msg": "后台占位行已结算",
                "data": {"task_id": task_id, "status": status, "count": len(settled)},
            },
        )
    return len(settled)
