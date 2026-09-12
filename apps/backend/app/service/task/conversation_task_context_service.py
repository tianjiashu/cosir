"""Task Agent context 的唯一 owner。"""

from __future__ import annotations

import copy

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.json_helpers import (
    TransportPart,
    TransportToolResult,
    empty_transport_metadata,
)
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud


class ConversationTaskContextService:
    """维护 LangChain 原生消息的 Task 级有序 context。

    以 ``conversation_task_contexts`` 表为唯一持久化真相，每条消息一行；
    运行时仅经 CRUD 读写，不存在进程内 context working copy（避免与数据库双写漂移）。

    职责边界：
    - 负责：消息与 Transport metadata 的追加、初始 user/system 幂等初始化、按 run 删除、
      序列号分配、纳入过滤读取。
    - 不负责：消息内容语义校验、压缩策略（由上下文管理器负责）。
    """

    def __init__(self) -> None:
        """绑定 context CRUD。"""

        self._crud = ConversationTaskContextCrud()

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: BaseMessage,
        seq: int | None = None,
        include_in_context: bool = True,
        *,
        transport_parts: list[TransportPart] | None = None,
        tool_result: TransportToolResult | None = None,
        session: Session | None = None,
    ) -> bool:
        """以独立事务追加一条完整 LangChain 消息。

        参数顺序保持 ``task_id, run_id, message, seq, include_in_context``，以兼容调用点的
        位置传参（``seq`` 为第 4 个位置参数）。

        参数:
            task_id: 目标任务标识。
            run_id: 产生消息的 Conversation Run 标识；system 消息可为 ``None``。
            message: 完整 LangChain 消息对象；不会被重建或裁剪字段。
            seq: 显式序号；为 ``None`` 时自动取 ``max_sequence(task_id) + 1``，
                避免与现有行 ``(task_id, sequence)`` 唯一约束冲突。
            include_in_context: 该消息是否纳入上下文视图，默认 ``True``。
            transport_parts: 完整消息对应的 Assistant Transport parts；由本 service
                组装并交给严格 metadata serializer，不允许调用方手写 metadata JSON。
            tool_result: ToolMessage 的结构化 Transport 结果；普通消息必须为 ``None``。
            session: 可选外部事务；传入时复用该事务，不自行提交。

        返回:
            ``True`` 表示新增；同一 run 的同一 tool_call_id 已存在时返回 ``False``。

        异常:
            持久化失败时由 CRUD 回滚并向上传播。

        副作用:
            新增一行上下文消息；``seq`` 为 ``None`` 时会产生一次 ``max_sequence`` 查询。
        """

        def all_records() -> list[ConversationTaskContextRecord]:
            if session is None:
                return self._crud.get(task_id, include_in_context=False)
            return self._crud.get(task_id, include_in_context=False, session=session)

        if isinstance(message, ToolMessage) and message.tool_call_id:
            existing = all_records()
            if any(
                record.run_id == run_id
                and isinstance(record.message, ToolMessage)
                and record.message.tool_call_id == message.tool_call_id
                for record in existing
            ):
                return False

        if seq is None:
            seq = (
                self._crud.max_sequence(task_id)
                if session is None
                else self._crud.max_sequence(task_id, session=session)
            )
            seq += 1
        metadata = empty_transport_metadata()
        if transport_parts is not None:
            metadata["parts"] = copy.deepcopy(transport_parts)
        if tool_result is not None:
            metadata["tool_result"] = copy.deepcopy(tool_result)
        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=run_id,
            message=message,
            include_in_context=include_in_context,
            sequence=seq,
            transport_metadata=metadata,
        )
        created = self._crud.create(record, session=session) is not False
        if created and session is None:
            log.info(
                "context_message_persisted",
                extra={
                    "msg": "canonical context message 已持久化",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "message_type": type(message).__name__,
                        "sequence": seq,
                    },
                },
            )
        return created

    def append_user_message_once(
        self,
        task_id: int,
        run_id: int,
        text: str,
        session: Session | None = None,
    ) -> bool:
        """持久化一次 Run 的初始 HumanMessage，并按 ``(task, run)`` 幂等。"""

        if session is None:
            existing = self._crud.get(task_id, include_in_context=False)
        else:
            existing = self._crud.get(task_id, include_in_context=False, session=session)
        if any(
            record.run_id == run_id and isinstance(record.message, HumanMessage)
            for record in existing
        ):
            return False
        return self.append(
            task_id,
            run_id,
            HumanMessage(content=text),
            session=session,
        )

    def ensure_system_message(
        self,
        task_id: int,
        message: SystemMessage,
        session: Session | None = None,
    ) -> bool:
        """确保 Task 只有一条历史 system prompt；已有历史内容永不静默替换。"""

        if session is None:
            existing = self._crud.get(task_id, include_in_context=False)
        else:
            existing = self._crud.get(task_id, include_in_context=False, session=session)
        if any(
            record.run_id is None and isinstance(record.message, SystemMessage)
            for record in existing
        ):
            return False
        return self.append(task_id, None, message, session=session)

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        """返回纳入上下文的 Task context entry 列表（仅取 ``include_in_context`` 为真）。"""

        records = self._crud.get(task_id) or []
        return [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
            )
            for record in records
        ]

    def entries(self, task_id: int) -> list[ContextEntry]:
        """返回 Task 全部 context entry（不分纳入标记）。"""

        records = self._crud.get(task_id, include_in_context=False) or []
        return [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
            )
            for record in records
        ]

    def reset_run_for_fresh(
        self, task_id: int, run_id: int, session: Session | None = None
    ) -> None:
        """清理 fresh 重试生成的消息，并保留已创建的 canonical HumanMessage 身份。"""

        self._crud.delete_generated_by_run_id(task_id, run_id, session=session)

    def max_sequence(self, task_id: int, session: Session | None = None) -> int:
        """返回 Task 当前最大 sequence；无记录时为 0。"""

        if session is None:
            return self._crud.max_sequence(task_id)
        return self._crud.max_sequence(task_id, session=session)

    def clone_for_fork(
        self,
        source_task_id: int,
        target_task_id: int,
        run_id_map: dict[int, int],
        session: Session,
    ) -> int:
        """在外部事务中复制指定 Run 前缀的全部 context entries。

        目标序号从 1 重新分配；序号数值不属于业务契约，只保证目标 Task 内严格递增且
        不重复。system prompt 是 Task 级事实，不属于任一 Run，因此不会从源 Task 复制。
        """

        source_entries = self._crud.get(
            source_task_id,
            include_in_context=False,
            session=session,
        )
        next_sequence = 1
        cloned_count = 0
        for source_entry in source_entries:
            if source_entry.run_id not in run_id_map:
                continue
            created = self._crud.create(
                ConversationTaskContextRecord(
                    task_id=target_task_id,
                    run_id=run_id_map[source_entry.run_id],
                    message=copy.deepcopy(source_entry.message),
                    include_in_context=source_entry.include_in_context,
                    sequence=next_sequence,
                    transport_metadata=copy.deepcopy(source_entry.transport_metadata),
                    message_schema_version=source_entry.message_schema_version,
                ),
                session=session,
            )
            if created:
                cloned_count += 1
            next_sequence += 1
        return cloned_count

    def delete_by_run_id(self, task_id: int, run_id: int, session: Session | None = None) -> None:
        """删除指定 run 的全部 context entry。

        参数:
            task_id: 归属的 Task 标识。
            run_id: 待删除的 Conversation Run 标识。

        返回:
            无。

        参数:
            session: 可选外部事务 Session；传入时复用该事务，不自行提交。

        副作用:
            删除匹配行；未传入 session 时由 CRUD 自建事务并提交。
        """

        self._crud.delete_by_run_id(task_id, run_id, session=session)
        if session is not None:
            # 外部事务通常关闭 autoflush；编辑用例随后会在同一 session 中重新写入
            # canonical HumanMessage，先 flush 才能让幂等读取看见删除事实。
            session.flush()

    def recover_interrupted_run(
        self, task_id: int, run_id: int, session: Session | None = None
    ) -> list[str]:
        """为崩溃遗留的未闭合 tool call 补写 context 终止消息。

        context 与 snapshot/Run 独立收敛；重复执行按 tool_call_id 幂等跳过。
        """

        entries = (
            self._entries_in_context_with_session(task_id, session)
            if session is not None
            else self.entries_in_context(task_id)
        )
        completed = {
            str(entry.message.tool_call_id)
            for entry in entries
            if entry.run_id == run_id and isinstance(entry.message, ToolMessage)
        }
        repaired: list[str] = []
        for entry in entries:
            if entry.run_id != run_id or not isinstance(entry.message, AIMessage):
                continue
            for tool_call in entry.message.tool_calls:
                call_id = str(tool_call.get("id") or "")
                if not call_id or call_id in completed:
                    continue
                created = self.append(
                    task_id,
                    run_id,
                    ToolMessage(
                        content="execution_interrupted",
                        tool_call_id=call_id,
                        status="error",
                        id=f"tool-{call_id}",
                    ),
                    session=session,
                    tool_result={
                        "status": "error",
                        "display_data": {"status_hint": "执行已中断"},
                        "status_hint": "执行已中断",
                        "error": "execution_interrupted",
                    },
                )
                if created:
                    repaired.append(call_id)
                    completed.add(call_id)
        return repaired

    def _entries_in_context_with_session(
        self, task_id: int, session: Session
    ) -> list[ContextEntry]:
        """在调用方事务内读取 context，避免恢复时读写跨越提交边界。"""

        records = self._crud.get(task_id, session=session) or []
        return [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
            )
            for record in records
        ]
