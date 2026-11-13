"""Task context 的跨行用例：观测读取、占位查询与结算、工具调用闭合、撤回目标定位。

行级读写与固定系统提示词归 ``ContextStore``；本服务只编排「跨多行、跨 Run 或跨 Task」的用例。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session

from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import (
    ContextStore,
)
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closures,
)
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import (
    TURN_INPUT_KINDS,
    ToolMessageMetadata,
    UserMessageMetadata,
)
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud


class ConversationTaskContextService:
    """Task 级有序 context 的跨行用例。

    以 ``conversation_task_contexts`` 表为唯一持久化事实源，每条完整消息占一行；
    ``RuntimeMessageContextManager`` 持有 Task 级内存工作副本，并经 ``ContextStore``
    读写持久化记录；观测页读取存储记录，不读取或替代运行时工作副本。

    职责边界：
    - 负责：观测口径读取、后台与 HITL 占位的查询与结算、为指定 Run 补工具中断占位、撤回目标定位。
      撤回本身归 ``turn_withdrawal_service``；fork 的切点与历史复制归 ``ForkSource``，由
      ``TaskService`` 在分叉事务里直接调用。
    - 不负责：单行追加与替换、序号推进、固定系统提示词（由 ``ContextStore`` 负责），
      消息内容语义校验与压缩策略（由上下文管理器负责）。
    """

    def __init__(self) -> None:
        """绑定 context CRUD、跨 Task/Run 读取所需 CRUD 与行级 store。"""

        self._crud = ConversationTaskContextCrud()
        self._task_crud = TaskCrud()
        self._run_crud = ConversationRunCrud()
        self._store = ContextStore()

    def list_observable_messages(
        self, task_id: int, *, turn: int | None = None
    ) -> tuple[list[ConversationTaskContextRecord], ConversationRunRecord | None]:
        """读取观测页消息（保留全部存储标记状态）；指定回合时只读这一回合，并给出所属的 Run。

        回合不跨 Run，所以这一回合的本地行属于同一个 Run；继承来的回合没有 Run，返回 ``None``。

        异常:
            KeyError: Task 不存在（映射 404）。
        """
        self._task_crud.get(task_id)
        rows = self._crud.get_for_observation(task_id, turn)
        run_id = next((row.run_id for row in rows if row.run_id is not None), None)
        if turn is None or run_id is None:
            return rows, None
        return rows, self._run_crud.get(run_id)

    def claimed_decisions(self, task_id: int) -> list[str]:
        """决定已认领、还在执行的 HITL 调用（占位 ``running``）；后台占位是 ``pending``，不在其中。"""
        return [
            str(record.message["tool_call_id"])
            for record in self._crud.list_unsettled_placeholders(task_id)
            if cast(ToolMessageMetadata, record.metadata).status == "running"
        ]

    def settle_placeholders(
        self,
        task_id: int | None,
        tool_call_ids: frozenset[str] | None,
        status: ToolCallEventStatus,
        error: str | None,
        *,
        exclude: frozenset[tuple[int, str]] = frozenset(),
        displays: dict[str, dict[str, object] | None] | None = None,
    ) -> list[ConversationTaskContextRecord]:
        """把还在等结果的占位行（``pending`` 的后台占位、``running`` 的 HITL 占位）推到终态，返回
        实际改写的行（新 metadata）。

        Run 之外的结算（撤销、重启）走这里；Run 内的结论并入由 workflow 经 context manager 结算。
        ``task_id`` 为 ``None`` 表示全部 Task，``tool_call_ids`` 为 ``None`` 表示不按调用过滤，
        ``exclude`` 里的调用不动（结论已在队列里，占位归那条结论结算）。只改 metadata、不改正文与
        可见性：占位仍是这次调用在模型协议上的结果。已到终态的行不动，迟到的结算因此幂等。
        ``displays`` 的非空项是执行器取消后的最终部分结果；缺失时保留此前展示。
        """

        settled: list[ConversationTaskContextRecord] = []
        ended_at = datetime.now(UTC).isoformat()
        for record in self._crud.list_unsettled_placeholders(task_id):
            tool_call_id = record.message.get("tool_call_id")
            if tool_call_ids is not None and tool_call_id not in tool_call_ids:
                continue
            if (record.task_id, tool_call_id) in exclude:
                continue
            previous = cast(ToolMessageMetadata, record.metadata)
            metadata = previous.model_copy(
                update={
                    "status": status,
                    "error": error,
                    "display_data": (
                        (displays or {}).get(str(tool_call_id)) or previous.display_data
                    ),
                    "background": previous.background.model_copy(update={"ended_at": ended_at})
                    if previous.background is not None
                    else None,
                }
            )
            updated = dataclasses.replace(record, metadata=metadata)
            self._crud.replace_message(record.task_id, record.sequence, updated)
            settled.append(updated)
        return settled

    def latest_user_input(
        self, task_id: int, session: Session | None = None
    ) -> ConversationTaskContextRecord | None:
        """最新一条输入是用户输入时返回它（撤回只允许撤它），否则返回 ``None``。

        按 metadata 的 ``kind`` 判断，不解析正文：通知、决定、后台结果同样是 user 角色。最新一条
        输入是 Agent 消息（含途中到达的）时也返回 ``None``：越过它撤回更早的输入会把它一起撤回，
        前端同样只让最新一条用户消息可撤回。已被撤回移出（不进上下文）的行不算。
        """
        for record in reversed(self._crud.get(task_id, include_in_context=True, session=session)):
            if record.run_id is None:
                continue
            metadata = record.metadata
            if isinstance(metadata, UserMessageMetadata) and metadata.kind in TURN_INPUT_KINDS:
                return record if metadata.kind == "user_input" else None
        return None

    def awaiting_decision_tool(
        self, task_id: int, tool_call_id: str, *, session: Session | None = None
    ) -> str | None:
        """这次调用仍在等用户决定时返回其工具名，否则返回 ``None``。

        等待与否的事实源是工具结果行：HITL 工具首次调用写入的等待占位 metadata 为 ``pending``；
        ``prepare_input`` 接收决定时在同一事务把它推到 ``running``，执行后由结算推到终态。所以
        「已决定、执行中」也不算等待，迟到的重复决定不会让原调用再重放一次。判定规则只在
        ``ConversationTaskContextRecord.awaits_user``（与侧栏「等你操作」同一条）。工具名取自发起
        这次调用的 assistant 消息，不依赖工具自己的业务事实（如交接登记）。
        """
        records = [
            r
            for r in self._crud.get(task_id, include_in_context=True, session=session)
            if r.run_id is not None
        ]
        result = next(
            (
                record
                for record in reversed(records)
                if record.message["role"] == "tool"
                and record.message["tool_call_id"] == tool_call_id
            ),
            None,
        )
        if result is None or not result.awaits_user:
            return None
        for record in reversed(records):
            if record.run_id is None or record.message["role"] != "assistant":
                continue
            for call in record.message.get("tool_calls") or ():
                if call["id"] == tool_call_id:
                    return call["function"]["name"]
        return None

    def close_unclosed_tool_calls_for_run(
        self, task_id: int, run_id: int, session: Session | None = None
    ) -> list[str]:
        """按完整配对计划为指定 Run 的缺失结果补中断占位，不重放工具。

        外部事务由恢复用例拥有。ContextProtocolError 表示原始调用依据不完整，此时不写任何
        占位；恢复用例保留原始记录并单独完成 Run 取消，避免一个坏 Task 阻断整个后端启动。
        结果只重排模型输入，不改持久序号；返回本次补建的调用 ID。
        """

        records = [
            r
            for r in self._crud.get(task_id, include_in_context=True, session=session)
            if r.run_id is not None
        ]
        entries = [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
                message_id=record.message_id,
                metadata=record.metadata,
                turn=record.turn,
            )
            for record in records
            if record.run_id == run_id
        ]
        plans = plan_tool_call_closures(entries)
        next_sequence = self._store.max_sequence(task_id, session=session) + 1
        repaired: list[str] = []
        for plan in plans:
            for slot in plan.missing_slots:
                # 占位属于发起调用的那一回合。
                self._store.append(
                    task_id,
                    run_id,
                    build_placeholder_tool_message(slot.call_id, slot.tool_name),
                    next_sequence,
                    True,
                    ToolMessageMetadata(status="cancelled"),
                    session=session,
                    turn=entries[plan.target_index].turn,
                )
                repaired.append(slot.call_id)
                next_sequence += 1
        return repaired
