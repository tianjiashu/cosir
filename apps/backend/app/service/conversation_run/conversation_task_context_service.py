"""Task context 的跨 Run 用例：观测读取、fork 复制、按 Run 删除与工具占位闭合。

行级读写与固定系统提示词归 ``ContextStore``；本服务只编排「跨多行、跨 Run 或跨 Task」的用例。
"""

from __future__ import annotations

import copy

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
from app.models.message_metadata import ToolMessageMetadata, UserMessageMetadata
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
        重建，避免 fork 后改变源 Task 的系统指令。只复制投影到前端的行（含不进上下文的中断输出），
        编辑替换掉的历史留在源 Task 供观测，不带进新 Task。``max_sequence`` 是切分点所在那一轮的
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
            # 不投影的行（编辑替换掉的历史）不属于源 Task 可见的会话，不复制；只投影不进上下文的
            # 行（中断输出、取消占位）照样复制，fork 后的前端历史与源 Task 一致。
            if (
                    source_entry.run_id not in run_id_map
                    or not source_entry.project_to_transport
                    or source_entry.sequence > max_sequence
            ):
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

    def exclude_from(self, task_id: int, sequence: int, session: Session) -> None:
        """编辑重跑：把 ``sequence`` 及其后的行移出模型上下文与前端投影。

        软删除：行保留供观测页查看。

        只能对最新一条用户输入调用：它之后的行都是对它的回答，一并移出。在调用方事务内执行，与新
        消息入队一起提交。``RuntimeMessageContextManager`` 的内存列表每次 ``begin_run`` 从库重建，
        之后的 Run 自然看不到这些行，不用通知它。
        """
        self._crud.exclude_from_sequence(task_id, sequence, session)

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
