"""Task Agent context 的唯一 owner。"""

from __future__ import annotations

import copy
from uuid import uuid4

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionSystemMessageParam
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.core.context.tool_call_closure import (
    build_placeholder_tool_message,
    plan_tool_call_closures,
)
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.message_metadata import MessageMetadata, ToolMessageMetadata, empty_message_metadata
from app.storage.crud.conversation_run_crud import ConversationRunCrud
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud

TASK_SYSTEM_PROMPT_SEQUENCE = 0


class ConversationTaskContextService:
    """维护 领域消息的 Task 级有序 context。

    以 ``conversation_task_contexts`` 表为唯一持久化事实源，每条完整消息占一行；
    assistant 中断输出也保存为一行，但不纳入模型上下文。
    ``RuntimeMessageContextManager`` 持有 Task 级内存工作副本，并经本服务
    读写持久化记录；观测页读取存储记录，不读取或替代运行时工作副本。

    职责边界：
    - 负责：固定 Task system prompt 的首次持久化、消息与 Transport metadata 的追加、
      工具占位原地替换、按 run 删除、序号查询、纳入过滤读取。
    - 不负责：消息内容语义校验、压缩策略（由上下文管理器负责）。
    """

    def __init__(self) -> None:
        """绑定 context CRUD。"""

        self._crud = ConversationTaskContextCrud()
        self._task_crud = TaskCrud()
        self._run_crud = ConversationRunCrud()

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

    def append(
        self,
        task_id: int,
        run_id: int | None,
        message: ChatCompletionMessageParam,
        seq: int,
        include_in_context: bool = True,
        metadata: MessageMetadata | None = None,
        project_to_transport: bool = True,
        session: Session | None = None,
        *,
        message_id: str | None = None,
    ) -> ContextEntry:
        """追加 SDK 正文并返回带稳定身份的 working-copy entry。

        有外部 Session 时加入用例事务，不自行提交；否则完成单次存储事务。
        用户消息沿用 inbox 行 ID 转成的展示身份，其余消息在此分配身份。调用方分配 sequence；
        本层不做工具去重，已有结果的覆盖由 RuntimeMessageContextManager 决定。
        数据库写入失败向上传播。
        """

        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=run_id,
            message=message,
            message_id=message_id or str(uuid4()),
            metadata=metadata if metadata is not None else empty_message_metadata(message["role"]),
            include_in_context=include_in_context,
            sequence=seq,
            project_to_transport=project_to_transport,
        )
        self._crud.create(record, session=session)
        if session is None:
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

        return ContextEntry(message, run_id, seq, record.message_id, record.metadata)

    def replace_message(
        self,
        record: ConversationTaskContextRecord,
        *,
        session: Session | None = None,
    ) -> None:
        """按稳定序号完整替换正文、标记和 metadata，不隐式保留旧展示字段。

        工具占位替换提交完整消息记录；调用方负责沿用消息身份。
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
                message_id=record.message_id,
                metadata=record.metadata,
            )
            for record in records
        ]

    def get_system_prompt(self, task_id: int) -> ContextEntry | None:
        """读取 Task 固定系统提示词；尚未初始化时返回 None。

        参数:
            task_id: 提示词所属 Task。

        返回:
            sequence 固定为 0、``run_id`` 为 None 的 canonical system entry；记录不存在时
            返回 None。提示词始终来自数据库，不因当前 profile 或配置变化而替换。

        异常:
            RuntimeError: sequence 0 已被非系统提示词记录占用，或记录违反 Task system
                prompt 的持久化约束。

        副作用:
            不修改数据库。
        """

        existing = self._crud.get_by_task_sequence(task_id, TASK_SYSTEM_PROMPT_SEQUENCE)
        if existing is None:
            return None
        return self._system_prompt_entry(task_id, existing)

    @staticmethod
    def _system_prompt_entry(
        task_id: int,
        record: ConversationTaskContextRecord,
    ) -> ContextEntry:
        """校验并映射 sequence 0 的 Task 系统提示词记录。"""

        if (
            record.run_id is not None
            or not record.message["role"] == "system"
            or not record.include_in_context
        ):
            raise RuntimeError(
                f"task {task_id} sequence {TASK_SYSTEM_PROMPT_SEQUENCE} "
                "is not a valid system prompt entry"
            )
        return ContextEntry(
            record.message, record.run_id, record.sequence, record.message_id, record.metadata
        )

    def create_system_prompt(
        self, task_id: int, prompt: ChatCompletionSystemMessageParam
    ) -> ContextEntry:
        """持久化新 Task 的完整系统提示词并返回其 canonical entry。

        参数:
            task_id: 提示词所属 Task。
            prompt: 由 ``SystemPromptBuilder`` 生成的完整系统提示词。

        返回:
            sequence 固定为 0、``run_id`` 为 None 的新 system entry。

        异常:
            sqlalchemy.exc.IntegrityError: sequence 0 已被并发初始化占用；调用方的 Task
                操作锁应保证此异常不发生。
            持久化异常: CRUD 写入失败时向上传播。

        副作用:
            新增一条纳入模型上下文的持久化记录并写入结构化日志。调用方须在 Task 操作边界内
            串行化首次初始化。
        """
        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=None,
            message=prompt,
            include_in_context=True,
            sequence=TASK_SYSTEM_PROMPT_SEQUENCE,
        )
        self._crud.create(record)
        log.info(
            "task_system_prompt_persisted",
            extra={
                "msg": "Task 首次系统提示词已持久化",
                "data": {"task_id": task_id, "sequence": TASK_SYSTEM_PROMPT_SEQUENCE},
            },
        )
        return ContextEntry(
            prompt, None, TASK_SYSTEM_PROMPT_SEQUENCE, record.message_id, record.metadata
        )

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
        """在外部事务中复制 Task 固定提示词和指定 Run 前缀的 context entries。

        system prompt 保留 sequence 0；Run context 从 1 开始重新分配。提示词不在目标 Task
        重建，避免 fork 后改变源 Task 的系统指令。
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
                self._system_prompt_entry(source_task_id, source_entry)
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
            if source_entry.run_id not in run_id_map:
                continue
            self._crud.create(
                ConversationTaskContextRecord(
                    task_id=target_task_id,
                    run_id=run_id_map[source_entry.run_id],
                    message=copy.deepcopy(source_entry.message),
                    message_id=source_entry.message_id,
                    metadata=copy.deepcopy(source_entry.metadata),
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
            # canonical user 消息，先 flush 才能让幂等读取看见删除事实。
            session.flush()

    def invalidate_waiting_calls(self, task_id: int, run_id: int, session: Session) -> None:
        """等待取消/启动恢复共用：取消占位并移除可点击请求，已完成结果保持原样。"""
        from dataclasses import replace

        for record in self._crud.get(task_id, include_in_context=False, session=session):
            if record.run_id != run_id or record.message["role"] != "tool":
                continue
            metadata = record.metadata
            if not isinstance(metadata, ToolMessageMetadata) or metadata.status not in {
                "pending",
                "running",
            }:
                continue
            replacement = replace(
                record,
                message=build_placeholder_tool_message(record.message["tool_call_id"], "unknown"),
                metadata=ToolMessageMetadata(status="cancelled", display_data=None, error=None),
            )
            self._crud.replace_message(task_id, record.sequence, replacement, session=session)

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
        next_sequence = self.max_sequence(task_id, session=session) + 1
        repaired: list[str] = []
        for slot in (slot for plan in plans for slot in plan.missing_slots):
            self.append(
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
