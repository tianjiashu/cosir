"""Task context 的跨 Run 用例：观测读取、fork 复制、按 Run 删除与工具占位闭合。

行级读写与固定系统提示词归 ``ContextStore``；本服务只编排「跨多行、跨 Run 或跨 Task」的用例。
"""

from __future__ import annotations

import copy
import dataclasses
from typing import cast

from sqlalchemy.orm import Session

from app.core.context.context_entry import ContextEntry
from app.core.context.context_store import (
    TASK_SYSTEM_PROMPT_SEQUENCE,
    ContextStore,
)
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closures,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.enums.tool_call_status import ToolCallEventStatus
from app.models.message_metadata import MessageMetadata, ToolMessageMetadata, UserMessageMetadata
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud


class ConversationTaskContextService:
    """维护 领域消息的 Task 级有序 context 的跨行用例。

    以 ``conversation_task_contexts`` 表为唯一持久化事实源，每条完整消息占一行；
    ``RuntimeMessageContextManager`` 持有 Task 级内存工作副本，并经 ``ContextStore``
    读写持久化记录；观测页读取存储记录，不读取或替代运行时工作副本。

    职责边界：
    - 负责：观测口径读取、按 Run 删除、fork 前缀复制、为指定 Run 补工具中断占位、编辑重跑的
      目标定位与软删除。
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
            self,
            *,
            task_id: int | None = None,
            run_id: int | None = None,
    ) -> list[ConversationTaskContextRecord]:
        """读取观测页消息；Task 或 Run 至少提供一个，并保留全部存储标记状态。"""

        if run_id is not None:
            run = self._run_crud.get(run_id)
            if task_id is not None and run.task_id != task_id:
                raise KeyError(run_id)
            resolved_task_id = run.task_id
        elif task_id is not None:
            self._task_crud.get(task_id)
            resolved_task_id = task_id
        else:
            raise ValueError("task_id or run_id is required")
        return self._crud.get_for_observation(resolved_task_id, run_id)

    def history(self, task_id: int, session: Session) -> list[ConversationTaskContextRecord]:
        """在外部事务中读取 Task 的全部 context 行（含不进上下文、不投影的行）。"""

        return self._crud.get(task_id, include_in_context=False, session=session)

    def clone_for_fork(
            self,
            source_task_id: int,
            target_task_id: int,
            run_id_map: dict[int, int],
            session: Session,
            *,
            max_sequence: int,
    ) -> int:
        """在外部事务中复制 Task 固定提示词和指定 Run 前缀里序号不晚于 ``max_sequence`` 的 entries。

        system prompt 保留 sequence 0；Run context 从 1 开始重新分配。提示词不在目标 Task
        重建，避免 fork 后改变源 Task 的系统指令。只复制仍属于会话的行（见
        :attr:`ConversationTaskContextRecord.retained`），编辑移出的历史留在源 Task 供观测，不带进新
        Task。``max_sequence`` 是切分点所在那一轮的
        最后一条：边界 Run 里更晚的轮次不复制。
        """

        source_entries = self._crud.get(
            source_task_id,
            include_in_context=False,
            session=session,
        )
        next_sequence = 1
        cloned_count = 0
        system_prompt_found = False
        for source_entry in source_entries:
            if source_entry.sequence == TASK_SYSTEM_PROMPT_SEQUENCE:
                self._store.to_system_prompt_entry(source_task_id, source_entry)
                self._crud.create(
                    ConversationTaskContextRecord(
                        task_id=target_task_id,
                        run_id=None,
                        message=copy.deepcopy(source_entry.message),
                        message_id=source_entry.message_id,
                        metadata=copy.deepcopy(source_entry.metadata),
                        include_in_context=True,
                        sequence=TASK_SYSTEM_PROMPT_SEQUENCE,
                        project_to_transport=source_entry.project_to_transport,
                    ),
                    session=session,
                )
                cloned_count += 1
                system_prompt_found = True
                continue
            # 编辑移出的行只留给源 Task 的观测页，不复制；其余行按原标志复制，fork 后的模型上下文
            # （含不投影的后台结论、用户决定、通知）与前端历史都与源 Task 一致。
            if (
                    source_entry.run_id not in run_id_map
                    or not source_entry.retained
                    or source_entry.sequence > max_sequence
            ):
                continue
            self._crud.create(
                ConversationTaskContextRecord(
                    task_id=target_task_id,
                    run_id=run_id_map[source_entry.run_id],
                    message=copy.deepcopy(source_entry.message),
                    message_id=source_entry.message_id,
                    metadata=_uninherited(source_entry.metadata),
                    include_in_context=source_entry.include_in_context,
                    sequence=next_sequence,
                    project_to_transport=source_entry.project_to_transport,
                ),
                session=session,
            )
            cloned_count += 1
            next_sequence += 1
        if not system_prompt_found:
            raise RuntimeError(f"task {source_task_id} has no persisted system prompt")
        return cloned_count

    def settle_placeholders(
            self,
            task_id: int | None,
            tool_call_ids: frozenset[str] | None,
            status: ToolCallEventStatus,
            error: str | None,
    ) -> list[ConversationTaskContextRecord]:
        """把还在等结果的占位行（``pending`` 的后台占位、``running`` 的 HITL 占位）推到终态，返回
        实际改写的行（新 metadata）。

        Run 之外的结算（撤销、重启）走这里；Run 内的结论并入由 workflow 经 context manager 结算。
        ``task_id`` 为 ``None`` 表示全部 Task，``tool_call_ids`` 为 ``None`` 表示不按调用过滤。只改
        metadata、不改正文与可见性：占位仍是这次调用在模型协议上的结果。已到终态的行不动，迟到
        的结算因此幂等。
        """

        settled: list[ConversationTaskContextRecord] = []
        for record in self._crud.list_unsettled_placeholders(task_id):
            tool_call_id = record.message.get("tool_call_id")
            if tool_call_ids is not None and tool_call_id not in tool_call_ids:
                continue
            metadata = cast(ToolMessageMetadata, record.metadata).model_copy(
                update={"status": status, "error": error}
            )
            updated = dataclasses.replace(record, metadata=metadata)
            self._crud.replace_message(record.task_id, record.sequence, updated)
            settled.append(updated)
        return settled

    def latest_user_input(
            self, task_id: int, session: Session | None = None
    ) -> ConversationTaskContextRecord | None:
        """最新一条已并入、仍投影的用户输入；编辑重跑只允许改它，没有时返回 ``None``。

        按 metadata 的 ``kind`` 判断是不是用户输入，不解析正文：通知、决定、后台结果
        同样是 user 角色。已被编辑移出（不进上下文）的行不算。
        """
        for record in reversed(self._crud.get(task_id, include_in_context=True, session=session)):
            metadata = record.metadata
            if isinstance(metadata, UserMessageMetadata) and metadata.kind == "user_input":
                return record
        return None

    def awaiting_decision_tool(
            self, task_id: int, tool_call_id: str, *, session: Session | None = None
    ) -> str | None:
        """这次调用仍在等用户决定时返回其工具名，否则返回 ``None``。

        等待与否的事实源是工具结果行：HITL 工具首次调用写入的等待占位 metadata 为 ``pending``；
        ``prepare_input`` 接收决定时在同一事务把它推到 ``running``，执行后由结算推到终态。所以
        「已决定、执行中」也不算等待，迟到的重复决定不会让原调用再重放一次。只看仍进模型上下文
        的行——被编辑重跑移出的调用已无法重放。工具名取自发起这次调用的 assistant 消息，不依赖
        工具自己的业务事实（如 TeamRun）。
        """
        records = self._crud.get(task_id, include_in_context=True, session=session)
        result = next(
            (
                record
                for record in reversed(records)
                if record.message["role"] == "tool"
                and record.message["tool_call_id"] == tool_call_id
            ),
            None,
        )
        if result is None or cast(ToolMessageMetadata, result.metadata).status != "pending":
            return None
        for record in reversed(records):
            if record.message["role"] != "assistant":
                continue
            for call in record.message.get("tool_calls") or ():
                if call["id"] == tool_call_id:
                    return call["function"]["name"]
        return None

    def has_tool_call(
            self,
            task_id: int,
            tool_names: frozenset[str],
            *,
            run_id: int | None = None,
    ) -> bool:
        """仍进模型上下文的 assistant 消息里是否调用过 ``tool_names`` 之一；``run_id`` 限定归属 Run。

        只看进上下文的行：模型看不到的调用（被编辑重跑移出）不需要再告知它的后果。
        """
        for record in self._crud.get(task_id, include_in_context=True):
            if record.message["role"] != "assistant":
                continue
            if run_id is not None and record.run_id != run_id:
                continue
            for call in record.message.get("tool_calls") or ():
                if call["type"] == "function" and call["function"]["name"] in tool_names:
                    return True
        return False

    def exclude_from(self, task_id: int, sequence: int, session: Session) -> frozenset[str]:
        """编辑重跑：撤回 ``sequence`` 起的这一轮，移出模型上下文与前端投影；返回被撤回的调用 id。

        软删除：行保留供观测页查看。只能对最新一条用户输入调用。

        按归属而不是只按序号移出：之后的行大多是对它的回答，但更早发起的调用的结果（决定、后台
        结论、HITL 执行结果）可能在这一轮才并入，序号排在它之后——它们回答的是之前的调用，保留，
        否则那次调用的占位已推到终态、结果却再也进不了上下文。归属读 metadata 的 ``tool_call_id``。

        返回的调用 id 是这一轮发起的全部调用，调用方据此撤销它们仍在进行的延迟工作。在调用方事务内
        执行，与新消息入队一起提交。``RuntimeMessageContextManager`` 的内存列表每次 ``begin_run``
        从库重建，之后的 Run 自然看不到这些行，不用通知它。
        """
        records = self._crud.get(task_id, include_in_context=False, session=session)
        earlier_calls: set[str] = set()
        withdrawn_calls: set[str] = set()
        for record in records:
            if record.message["role"] != "assistant":
                continue
            calls = withdrawn_calls if record.sequence >= sequence else earlier_calls
            calls.update(call["id"] for call in record.message.get("tool_calls") or ())
        kept_ids = frozenset(
            cast(int, record.id)
            for record in records
            if record.sequence >= sequence
            and isinstance(record.metadata, UserMessageMetadata)
            and record.metadata.tool_call_id in earlier_calls
        )
        self._crud.exclude_from_sequence(task_id, sequence, session, keep_ids=kept_ids)
        return frozenset(withdrawn_calls)

    def close_unclosed_tool_calls_for_run(
            self, task_id: int, run_id: int, session: Session | None = None
    ) -> list[str]:
        """按完整配对计划为指定 Run 的缺失结果补中断占位，不重放工具。

        外部事务由恢复用例拥有。ContextProtocolError 表示原始调用依据不完整，此时不写任何
        占位；恢复用例保留原始记录并单独完成 Run 取消，避免一个坏 Task 阻断整个后端启动。
        结果只重排模型输入，不改持久序号；返回本次补建的调用 ID。
        """

        records = self._crud.get(task_id, include_in_context=True, session=session)
        entries = [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
                message_id=record.message_id,
                metadata=record.metadata,
            )
            for record in records
            if record.run_id == run_id
        ]
        plans = plan_tool_call_closures(entries)
        next_sequence = self._store.max_sequence(task_id, session=session) + 1
        repaired: list[str] = []
        for slot in (slot for plan in plans for slot in plan.missing_slots):
            self._store.append(
                task_id,
                run_id,
                build_placeholder_tool_message(slot.call_id, slot.tool_name),
                next_sequence,
                True,
                ToolMessageMetadata(status="cancelled"),
                session=session,
            )
            repaired.append(slot.call_id)
            next_sequence += 1
        return repaired


def _uninherited(metadata: MessageMetadata) -> MessageMetadata:
    """fork 复制时，仍在等结论的后台占位改为 ``cancelled``：后台工作只属于源 Task，新 Task 收不到
    它的结论，卡片以短提示「未继承」说明，不另设状态。"""

    copied = copy.deepcopy(metadata)
    if isinstance(copied, ToolMessageMetadata) and copied.deferred and copied.status == "pending":
        return copied.model_copy(update={"status": "cancelled", "error": "未继承"})
    return copied
