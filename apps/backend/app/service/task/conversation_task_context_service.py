"""Task Agent context 的唯一 owner。"""

from __future__ import annotations

import copy

from langchain_core.messages import BaseMessage
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closure,
)
from app.models.conversation_task_context import ConversationTaskContextRecord, TransportMetadata
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud


class ConversationTaskContextService:
    """维护 LangChain 原生消息的 Task 级有序 context。

    以 ``conversation_task_contexts`` 表为唯一持久化真相，每条完整消息或 assistant
    partial 草稿一行；
    运行时仅经 CRUD 读写，不存在进程内 context working copy（避免与数据库双写漂移）。

    职责边界：
    - 负责：消息与 Transport metadata 的追加、assistant 草稿原地替换、按 run 删除、
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
        transport_metadata: TransportMetadata | None = None,
        is_streaming: bool = False,
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
            is_streaming: 是否为仅供 Transport 冷重建的 assistant 流式草稿。草稿必须同时
                使用 ``include_in_context=False``，避免半截消息进入下一次模型请求。
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


        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=run_id,
            message=message,
            include_in_context=include_in_context,
            sequence=seq,
            transport_metadata=transport_metadata,
            is_streaming=is_streaming,
        )
        if is_streaming and include_in_context:
            raise ValueError("streaming context messages must not enter model context")
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

    def streaming_messages_for_run(
        self, task_id: int, run_id: int
    ) -> list[ConversationTaskContextRecord]:
        """返回指定 Run 尚未收口的流式 assistant 草稿，按 sequence 排序。"""

        return [
            record
            for record in self._crud.get(task_id, include_in_context=False)
            if record.run_id == run_id and record.is_streaming
        ]

    def replace_streaming_message(
        self,
        record: ConversationTaskContextRecord,
        *,
        session: Session | None = None,
    ) -> None:
        """更新一条已存在的流式草稿或将其收口为完整消息。

        ``record.sequence`` 是流式消息的稳定身份；方法不新增行，避免每个 chunk 形成一
        条上下文消息。持久化层异常向调用方传播，由模型节点的运行终态处理。
        """

        self._crud.replace_message(
            record.task_id,
            record.sequence,
            record,
            session=session,
        )

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
                    is_streaming=source_entry.is_streaming,
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

    def close_unclosed_tool_calls_for_run(
        self, task_id: int, run_id: int, session: Session | None = None
    ) -> list[str]:
        """为指定 Run 最后一个 ``AIMessage`` 上未产生结果的调用补 ``cancelled`` 占位。

        供进程重启恢复（``ConversationRunService.recover_orphaned_runs``）使用：崩溃或被强杀
        会让该 Run 的 ``AIMessage(tool_calls)`` 没有结果行，模型协议因此不闭合。本方法只补
        缺失的占位事实，不重排、不删除已有行：

        - 已有结果与占位的相对顺序由运行时 ``RuntimeContextManager`` 在取数时修复；
        - 占位归属该 Run（与调用同 Run），冷读快照按 Run 分组配对才不会错位。

        配对规则与运行时收口共用 ``plan_tool_call_closure``，避免两处规则漂移。

        参数:
            task_id: 归属的 Task 标识。
            run_id: 待收口的 Conversation Run 标识。
            session: 可选外部事务；传入时复用该事务且不自行提交（恢复流程与 Run 终态同事务）。

        返回:
            本次新增占位的 ``tool_call_id`` 列表（按 ``tool_calls`` 顺序）；无未配对调用、
            或同名结果行已存在时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 持久化失败时向上传播，由调用方事务回滚。

        副作用:
            为该 Run 中未产生结果的调用各追加一行 ``ToolMessage``（``include_in_context=True``、
            ``TransportMetadata(status="cancelled")``）并推进序号。
        """

        records = self._crud.get(task_id, include_in_context=True, session=session)
        entries = [
            ContextEntry(
                run_id=record.run_id,
                message=record.message,
                sequence=record.sequence,
            )
            for record in records
            if record.run_id == run_id
        ]
        plan = plan_tool_call_closure(entries)
        if plan is None or not plan.missing_slots:
            return []

        next_sequence = self.max_sequence(task_id, session=session) + 1
        repaired: list[str] = []
        for slot in plan.missing_slots:
            created = self.append(
                task_id,
                run_id,
                build_placeholder_tool_message(slot.call_id, slot.tool_name),
                next_sequence,
                True,
                TransportMetadata(status="cancelled"),
                session=session,
            )
            if created is False:
                continue
            repaired.append(slot.call_id)
            next_sequence += 1
        return repaired
