"""Run 之外结算还在等结果的占位行：撤销、用户取消与重启（设计文档「卡片状态」）。

占位行是卡片状态的事实源：后台占位以 ``pending`` 写入，结论并入时由 workflow 在 Run
内推到 ``completed``（见 ``tool_observation_projection.settle_background_placeholder``）；HITL 等待占位
接收决定时推到 ``running``，执行结果由 ``settle_decision_observation`` 结算。这里处理不在
Run 内发生的结算：写库后经 projector 投影，但只投影到已物化的 snapshot——重启收尾会扫过全部 Session，
不能为每个 Session 物化 space 与 actor；未物化的 snapshot 下次重建自会读到占位行的新状态。投影失败只
记录，理由相同。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from app.config.logging.logger import log
from app.core.events import ToolCallStatusChangedEvent
from app.core.events.event_sink import event_sink
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import ToolMessageMetadata
from app.models.session_context import SessionContextRecord
from app.service import depends as service_depends

# 卡片短提示（约 5 字）：区分「被谁结束」，状态本身都是 ``cancelled``。
USER_CANCELLED_HINT = "已取消"
RUN_STOPPED_HINT = "已停止"
INTERRUPTED_HINT = "已中断"


def cancel_placeholders(
    session_id: int | None,
    tool_call_ids: Iterable[str] | None,
    hint: str | None,
    *,
    exclude: frozenset[tuple[int, str]] = frozenset(),
    displays: dict[str, dict[str, object] | None] | None = None,
) -> frozenset[int]:
    """把还在等结果的占位（``pending`` 的后台占位、``running`` 的 HITL 占位）结束为
    ``cancelled``，返回**被改写的占位所属的 Session**。``hint`` 只说明被谁结束（停止 / 取消 / 重启）。

    ``session_id`` 为 ``None`` 时跨全部 Session（重启）；``tool_call_ids`` 为 ``None`` 时不按调用过滤；
    ``exclude`` 里的调用不动（结论已在队列里，占位归那条结论结算）。
    ``displays`` 按调用 ID 提供执行器最终部分结果，只写回原占位 metadata，不投递消息。
    返回 Session 集合而不是行数：重启收尾据此给这些 Session 投重启通知，行数对调用方没有用。
    同步写库，调用方放在工作线程。
    """

    ids = None if tool_call_ids is None else frozenset(tool_call_ids)
    if ids is not None and not ids:
        return frozenset()
    status: ToolCallEventStatus = "cancelled"
    settled = service_depends.get_session_context_service().settle_placeholders(
        session_id, ids, status, hint, exclude=exclude, displays=displays
    )
    publish_settled(settled, status, hint)
    if settled:
        log.info(
            "background_placeholders_settled",
            extra={
                "msg": "后台占位行已结算",
                "data": {"session_id": session_id, "status": status, "count": len(settled)},
            },
        )
    return frozenset(record.session_id for record in settled)


def publish_settled(
    settled: Iterable[SessionContextRecord], status: ToolCallEventStatus, hint: str | None
) -> None:
    """把 Run 之外结算的占位投影到已物化的 snapshot；未物化的下次重建自会读到（见模块说明）。

    本模块的 ``cancel_placeholders`` 与等用户回应的决定的作废（``decision_expiry``）共用。
    """
    from app.session_runtime.session_runtime_space_registry import session_runtime_spaces

    sink = event_sink()
    for record in settled:
        tool_call_id = str(record.message.get("tool_call_id"))
        space = session_runtime_spaces.get(record.session_id)
        if space is None or not space.has_snapshot():
            continue
        metadata = cast(ToolMessageMetadata, record.metadata)
        try:
            sink(
                ToolCallStatusChangedEvent(
                    session_id=record.session_id,
                    run_id=record.run_id,
                    tool_call_id=tool_call_id,
                    status=status,
                    error=hint,
                    display_data=metadata.display_data,
                    background=metadata.background,
                )
            )
        except (KeyError, ValueError):
            log.warning(
                "background_placeholder_projection_skipped",
                extra={
                    "msg": "占位已结算，snapshot 投影跳过（下次重建读到）",
                    "data": {"session_id": record.session_id, "tool_call_id": tool_call_id},
                },
                exc_info=True,
            )
