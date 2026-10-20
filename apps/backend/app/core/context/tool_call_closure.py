"""工具调用与结果的纯配对规则，不执行工具、不修改消息事实。

按 Run 和先后次序配对全部调用批次。已存在的结果必须有前置调用，且每条调用只能有一个
结果；缺结果可按真实调用补中断占位，缺调用则无法重建原始参数，必须显式报错。
运行时与重启恢复共用计划；结果重排只改变模型输入顺序，不改写持久化序号。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import cast

from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionToolMessageParam,
)

from app.core.context.context_entry import ContextEntry


@dataclass(frozen=True)
class ToolCallSlot:
    """一个 ``tool_call`` 的配对结果。

    属性:
        call_id: 已通过配对唯一性检查的调用标识。
        tool_name: 调用名，仅用于占位文案。
        tool_index: 已落库结果在输入序列中的下标；``None`` 表示结果缺失、需要补占位。
    """

    call_id: str
    tool_name: str
    tool_index: int | None


@dataclass(frozen=True)
class ToolCallClosurePlan:
    """一条 ``assistant 消息`` 的收口计划。

    属性:
        target_index: 目标 ``assistant 消息`` 在输入序列中的下标。
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


class ContextProtocolError(RuntimeError):
    """上下文缺少可靠配对依据；保留原始事实，阻止模型调用或无依据的结果写入。"""

    def __init__(self, reason: str, entry: ContextEntry, call_id: str) -> None:
        self.reason = reason
        self.entry = entry
        self.call_id = call_id
        super().__init__(
            f"{reason}: run={entry.run_id}, sequence={entry.sequence}, tool_call_id={call_id}"
        )

    def log_data(self) -> dict[str, str | int | None]:
        """只提供定位字段；结果正文仍留在 canonical context，不重复写进错误载荷。"""
        return {
            "reason": self.reason,
            "context_run_id": self.entry.run_id,
            "sequence": self.entry.sequence,
            "message_id": self.entry.message_id,
            "tool_call_id": self.call_id,
        }


def plan_tool_call_closures(entries: Sequence[ContextEntry]) -> tuple[ToolCallClosurePlan, ...]:
    """为全部 assistant 调用批次配对，拒绝孤儿、重复结果及含糊的调用身份。

    只接收纳入模型上下文的条目。调用依据必须在结果之前且同属一个 Run；不会把后出现的
    同名调用倒配给旧结果。结果允许被用户消息等隔开，由运行时按计划恢复协议顺序。
    抛出的 ContextProtocolError 由运行时或恢复用例记录定位信息并承担失败语义。
    """
    batches: list[tuple[int, list[ToolCallSlot]]] = []
    owners: dict[tuple[int | None, str], tuple[int, int]] = {}
    for index, entry in enumerate(entries):
        message = entry.message
        if message["role"] == "assistant" and message.get("tool_calls"):
            slots: list[ToolCallSlot] = []
            batch_index = len(batches)
            batches.append((index, slots))
            for call in cast(
                list[ChatCompletionMessageFunctionToolCallParam], message["tool_calls"]
            ):
                call_id = call["id"]
                key = (entry.run_id, call_id)
                if not call_id:
                    raise ContextProtocolError("invalid_tool_call_id", entry, call_id)
                previous = owners.get(key)
                if previous is not None:
                    old_batch, old_slot = previous
                    if batches[old_batch][1][old_slot].tool_index is None:
                        raise ContextProtocolError("ambiguous_tool_call_id", entry, call_id)
                owners[key] = (batch_index, len(slots))
                slots.append(ToolCallSlot(call_id, call["function"]["name"], None))
        elif message["role"] == "tool":
            call_id = message["tool_call_id"]
            owner = owners.get((entry.run_id, call_id))
            if owner is None:
                raise ContextProtocolError("orphan_tool_result", entry, call_id)
            batch, position = owner
            slot = batches[batch][1][position]
            if slot.tool_index is not None:
                raise ContextProtocolError("duplicate_tool_result", entry, call_id)
            batches[batch][1][position] = replace(slot, tool_index=index)
    return tuple(
        ToolCallClosurePlan(index, entries[index].run_id, tuple(slots)) for index, slots in batches
    )


def build_placeholder_tool_message(call_id: str, tool_name: str) -> ChatCompletionToolMessageParam:
    """构造「调用未产生结果」的占位 ``tool 消息``。

    文案面向模型、纯英文，说明该调用因 Run 取消或中断没有结果。调用方负责随行写入
    ``ToolMessageMetadata(status="cancelled")`` 与调用所属 ``run_id``。

    参数:
        call_id: 未闭合的调用标识。
        tool_name: 调用名；未知时传 ``"unknown"``。

    返回:
        可直接写入上下文的 ``tool 消息``。

    异常:
        无。

    副作用:
        无。
    """
    return ChatCompletionToolMessageParam(
        role="tool",
        content=f"The tool call '{tool_name}' (id={call_id}) did not produce a result because "
        f"the run was cancelled or interrupted; no tool output is available.",
        tool_call_id=call_id,
    )


def build_waiting_input_tool_message(
    call_id: str, tool_name: str
) -> ChatCompletionToolMessageParam:
    """构造「调用正在等待用户决定」的占位 ``tool 消息``。

    与 :func:`build_placeholder_tool_message` 的区别：本占位表示调用**尚未执行**、正在等用户作答，
    而不是已取消 / 中断。它让该调用在模型协议上成为「已配对」，从而：

    - 启动期崩溃恢复不会再为它补一条 ``cancelled`` 占位（那会谎报调用已取消）；
    - 冷重建（进程重启 / 前端重连）能从该行的 ``metadata`` 恢复审批卡片与表单——
      part 的动态展示数据只从这张表读取（见 ``ConversationTaskStateRebuilder``）。

    文案面向模型、纯英文，且不说「失败 / 取消」：万一某条路径让模型在等待期间看到它，模型会知道
    调用还没执行，而不是重发一次。

    参数:
        call_id: 等待用户决定的调用标识。
        tool_name: 调用名；未知时传 ``"unknown"``。

    返回:
        可直接写入上下文的 ``tool 消息``。

    异常:
        无。

    副作用:
        无。
    """

    return {
        "role": "tool",
        "content": f"The tool call '{tool_name}' (id={call_id}) has not executed yet because it is "
        f"waiting for the user's decision; no tool output is available.",
        "tool_call_id": call_id,
    }
