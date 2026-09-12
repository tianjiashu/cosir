"""未闭合工具调用的配对规则（纯函数）。

单一职责：给定一段按落库顺序排列的上下文条目，定位**唯一可能未闭合**的那条
``AIMessage``（最后一条携带 ``tool_calls`` 的消息），给出每个 ``tool_call`` 的配对结果，
并提供「调用未产生结果」的占位消息构造。本模块不做 IO、不写日志、不修改入参。

为什么只可能是最后一条：一次模型调用只产出一条 ``AIMessage``，而每个 model 步入场都会先
收口一次，未配对调用不跨 model 步留存，更早的消息不可能仍未闭合。

调用方（同一规则，两处复用，避免规则漂移）：
- ``RuntimeContextManager._close_unclosed_tool_calls``：运行时按计划把已落库结果搬回目标消息
  之后，并为缺失的调用补占位（写内存工作副本与数据库）；
- ``ConversationTaskContextService.close_unclosed_tool_calls_for_run``：进程重启恢复时按计划
  仅为缺失的调用补占位（只写数据库，不重排）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, ToolMessage

from app.core.context.context_entry import ContextEntry

# 占位文案中工具名缺省值：调用未携带 name 时使用。
_UNKNOWN_TOOL_NAME = "unknown"


@dataclass(frozen=True)
class ToolCallSlot:
    """一个 ``tool_call`` 的配对结果。

    属性:
        call_id: 模型给出的调用标识；未携带 id 的调用不会进入计划（协议上无需配对）。
        tool_name: 调用名，仅用于占位文案。
        tool_index: 已落库结果在输入序列中的下标；``None`` 表示结果缺失、需要补占位。
    """

    call_id: str
    tool_name: str
    tool_index: int | None


@dataclass(frozen=True)
class ToolCallClosurePlan:
    """一条 ``AIMessage`` 的收口计划。

    属性:
        target_index: 目标 ``AIMessage`` 在输入序列中的下标。
        target_run_id: 目标消息所属 Run；占位必须归属同一个 Run，否则冷读快照按 Run 分组
            配对时会看到一条没有对应 AI 调用的工具结果。
        slots: 按 ``tool_calls`` 顺序排列的配对结果。
    """

    target_index: int
    target_run_id: int | None
    slots: tuple[ToolCallSlot, ...]

    @property
    def claimed_tool_indices(self) -> tuple[int, ...]:
        """已配对结果的下标（按 ``tool_calls`` 顺序）；它们必须紧跟目标消息。"""

        return tuple(slot.tool_index for slot in self.slots if slot.tool_index is not None)

    @property
    def missing_slots(self) -> tuple[ToolCallSlot, ...]:
        """结果缺失、需要补 ``cancelled`` 占位的调用（按 ``tool_calls`` 顺序）。"""

        return tuple(slot for slot in self.slots if slot.tool_index is None)


def plan_tool_call_closure(entries: Sequence[ContextEntry]) -> ToolCallClosurePlan | None:
    """给出输入序列中最后一条携带 ``tool_calls`` 的 ``AIMessage`` 的收口计划。

    参数:
        entries: 按落库顺序排列的上下文条目；归属范围由调用方决定（运行时传全部纳入上下文的
            条目，启动恢复只传目标 Run 的条目）。

    返回:
        收口计划；序列中没有携带 ``tool_calls`` 的 ``AIMessage`` 时返回 ``None``。

    异常:
        无。

    副作用:
        无（纯函数，不修改入参）。
    """

    target_index: int | None = None
    target_message: AIMessage | None = None
    for index in range(len(entries) - 1, -1, -1):
        message = entries[index].message
        if isinstance(message, AIMessage) and message.tool_calls:
            target_index = index
            target_message = message
            break
    if target_index is None or target_message is None:
        return None

    # 只有目标消息之后的结果才可能属于它；同一 call_id 多行时按落库顺序先到先用。
    tool_indices_by_call_id: dict[str, list[int]] = {}
    for index in range(target_index + 1, len(entries)):
        message = entries[index].message
        if isinstance(message, ToolMessage) and message.tool_call_id:
            tool_indices_by_call_id.setdefault(message.tool_call_id, []).append(index)

    claimed_tool_indices: set[int] = set()
    slots: list[ToolCallSlot] = []
    for call in target_message.tool_calls:
        call_id = call.get("id")
        if not call_id:
            continue
        available = [
            index
            for index in tool_indices_by_call_id.get(call_id, [])
            if index not in claimed_tool_indices
        ]
        tool_index = available[0] if available else None
        if tool_index is not None:
            claimed_tool_indices.add(tool_index)
        slots.append(
            ToolCallSlot(
                call_id=call_id,
                tool_name=call.get("name") or _UNKNOWN_TOOL_NAME,
                tool_index=tool_index,
            )
        )
    return ToolCallClosurePlan(
        target_index=target_index,
        target_run_id=entries[target_index].run_id,
        slots=tuple(slots),
    )


def build_placeholder_tool_message(call_id: str, tool_name: str) -> ToolMessage:
    """构造「调用未产生结果」的占位 ``ToolMessage``。

    文案面向模型、纯英文，说明该调用因 Run 取消或中断没有结果。调用方负责随行写入
    ``TransportMetadata(status="cancelled")`` 与调用所属 ``run_id``。

    参数:
        call_id: 未闭合的调用标识。
        tool_name: 调用名；未知时传 ``"unknown"``。

    返回:
        可直接写入上下文的 ``ToolMessage``。

    异常:
        无。

    副作用:
        无。
    """

    return ToolMessage(
        content=(
            f"The tool call '{tool_name}' (id={call_id}) did not produce a result because "
            f"the run was cancelled or interrupted; no tool output is available."
        ),
        tool_call_id=call_id,
    )
