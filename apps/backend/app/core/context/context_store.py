"""Task context 的持久化读写边界：行级追加、原地替换、序号与固定系统提示词。

本模块只负责「把一条完整消息写成 ``conversation_task_contexts`` 事实，并把它读成
``ContextEntry``」；不持有内存 working copy、不做工具配对、不决定消息是否接纳。序号由调用方
分配，读取口径由 ``include_in_context`` 决定。无进程内状态，可随时构造。
"""

from __future__ import annotations

from typing import cast

from openai.types.chat import (
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
)
from sqlalchemy.orm import Session

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.models.conversation_task_context import (
    ConversationTaskContextRecord,
    run_last_turn_withdrawn,
)
from app.models.message_metadata import MessageMetadata, empty_message_metadata
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud

# Task 固定系统提示词占用 sequence 0 与回合 0，不参与压缩、编辑与 Run、回合划分。
TASK_SYSTEM_PROMPT_SEQUENCE = 0
TASK_SYSTEM_PROMPT_TURN = 0


class ContextStore:
    """Task 级 Agent context 的持久化读写入口（无内存状态，只包 context CRUD）。"""

    def __init__(self) -> None:
        """绑定 context CRUD；不持有任何进程内缓存。"""

        self._crud = ConversationTaskContextCrud()

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
        turn: int,
        message_id: int | None = None,
    ) -> ContextEntry:
        """追加 SDK 正文并返回带稳定身份的 working-copy entry。

        有外部 Session 时加入用例事务，不自行提交；否则完成单次存储事务。
        用户消息沿用 message_queue 行 id 作为身份，其余消息没有身份（``None``）。调用方分配 sequence
        与回合号（回合由 RuntimeMessageContextManager 判定，本层只照写）；本层不做工具去重，已有
        结果的覆盖由 RuntimeMessageContextManager 决定。数据库写入失败向上传播。
        """

        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=run_id,
            message=message,
            message_id=message_id,
            metadata=metadata if metadata is not None else empty_message_metadata(message["role"]),
            include_in_context=include_in_context,
            sequence=seq,
            turn=turn,
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

        return ContextEntry(message, run_id, seq, record.message_id, record.metadata, turn=turn)

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
                turn=record.turn,
            )
            for record in records
        ]

    def tool_call_ids(self, task_id: int) -> set[str]:
        """Task 里出现过的全部调用 id，含已移出上下文的行：登记表与观测页还可能引用它们。"""

        return {
            call["id"]
            for record in self._crud.get(task_id, include_in_context=False)
            for call in cast(
                list[ChatCompletionMessageFunctionToolCallParam],
                record.message.get("tool_calls") or [],
            )
        }

    def max_sequence(self, task_id: int, session: Session | None = None) -> int:
        """返回 Task 当前最大 sequence；无记录时为 0。"""

        if session is None:
            return self._crud.max_sequence(task_id)
        return self._crud.max_sequence(task_id, session=session)

    def run_records(self, task_id: int, run_id: int) -> list[ConversationTaskContextRecord]:
        """这个 Run 已写下的全部行（含不进上下文的草稿与中断输出），按序号升序。"""

        return [
            record
            for record in self._crud.get(task_id, include_in_context=False)
            if record.run_id == run_id
        ]

    def run_last_turn_withdrawn(self, task_id: int, run_id: int) -> bool:
        """这个 Run 的最后一个回合是否已被撤回（规则见 ``run_last_turn_withdrawn`` 纯函数）。"""
        return run_last_turn_withdrawn(self.run_records(task_id, run_id))

    def max_turn(self, task_id: int) -> int:
        """返回 Task 当前最大回合号（含已被编辑移出的行）；无对话行时为 0。"""

        return self._crud.max_turn(task_id)

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
        return self.to_system_prompt_entry(task_id, existing)

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
            turn=TASK_SYSTEM_PROMPT_TURN,
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
            prompt,
            None,
            TASK_SYSTEM_PROMPT_SEQUENCE,
            record.message_id,
            record.metadata,
            turn=TASK_SYSTEM_PROMPT_TURN,
        )

    @staticmethod
    def to_system_prompt_entry(
        task_id: int,
        record: ConversationTaskContextRecord,
    ) -> ContextEntry:
        """校验并映射 sequence 0 的 Task 系统提示词记录；读取与 fork 复制共用同一约束。"""

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
            record.message,
            record.run_id,
            record.sequence,
            record.message_id,
            record.metadata,
            turn=record.turn,
        )
