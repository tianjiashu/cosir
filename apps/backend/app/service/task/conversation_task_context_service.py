"""Task Agent context 的唯一 owner。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.core.context.context_entry import ContextEntry
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud


class ConversationTaskContextService:
    """维护 LangChain 原生消息的 Task 级有序 context。

    以 ``conversation_task_contexts`` 表为唯一持久化真相，每条消息一行；
    运行时仅经 CRUD 读写，不存在进程内 context working copy（避免与数据库双写漂移）。

    职责边界：
    - 负责：消息的追加、按 run 删除、序列号分配、纳入过滤读取。
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
    ) -> None:
        """以独立事务追加一条完整 LangChain 消息。

        参数顺序保持 ``task_id, run_id, message, seq, include_in_context``，以兼容调用点的
        位置传参（``seq`` 为第 4 个位置参数）。

        参数:
            task_id: 目标任务标识。
            run_id: 产生消息的 Conversation Run 标识；system 消息可为 ``None``。
            message: 完整 LangChain 消息对象。
            seq: 显式序号；为 ``None`` 时自动取 ``max_sequence(task_id) + 1``，
                避免与现有行 ``(task_id, sequence)`` 唯一约束冲突。
            include_in_context: 该消息是否纳入上下文视图，默认 ``True``。

        返回:
            无。

        异常:
            持久化失败时由 CRUD 回滚并向上传播。

        副作用:
            新增一行上下文消息；``seq`` 为 ``None`` 时会产生一次 ``max_sequence`` 查询。
        """

        if seq is None:
            seq = self._crud.max_sequence(task_id) + 1
        record = ConversationTaskContextRecord(
            task_id=task_id,
            run_id=run_id,
            message=message,
            include_in_context=include_in_context,
            sequence=seq,
        )
        self._crud.create(record)

    def entries_in_context(self, task_id: int) -> list[ContextEntry]:
        """返回纳入上下文的 Task context entry 列表（仅取 ``include_in_context`` 为真）。"""

        records = self._crud.get(task_id) or []
        return [ContextEntry(run_id=record.run_id, message=record.message) for record in records]

    def entries(self, task_id: int) -> list[ContextEntry]:
        """返回 Task 全部 context entry（不分纳入标记）。"""

        records = self._crud.get(task_id, include_in_context=False) or []
        return [ContextEntry(run_id=record.run_id, message=record.message) for record in records]

    def max_sequence(self, task_id: int) -> int:
        """返回 Task 当前最大 sequence；无记录时为 0。"""

        return self._crud.max_sequence(task_id)

    def delete_by_run_id(self, task_id: int, run_id: int) -> None:
        """删除指定 run 的全部 context entry。

        参数:
            task_id: 归属的 Task 标识。
            run_id: 待删除的 Conversation Run 标识。

        返回:
            无。

        副作用:
            删除匹配行；由 CRUD 自建事务并提交。
        """

        self._crud.delete_by_run_id(task_id, run_id)

    def recover_interrupted_run(self, task_id: int, run_id: int) -> None:
        """为崩溃遗留的未闭合 tool call 补写 context 终止消息。

        context 与 snapshot/Run 独立收敛；重复执行按 tool_call_id 幂等跳过。
        """

        entries = self.entries_in_context(task_id)
        completed = {
            str(entry.message.tool_call_id)
            for entry in entries
            if entry.run_id == run_id and isinstance(entry.message, ToolMessage)
        }
        for entry in entries:
            if entry.run_id != run_id or not isinstance(entry.message, AIMessage):
                continue
            for tool_call in entry.message.tool_calls:
                call_id = str(tool_call.get("id") or "")
                if not call_id or call_id in completed:
                    continue
                self.append(
                    task_id,
                    run_id,
                    ToolMessage(
                        content="execution_interrupted",
                        tool_call_id=call_id,
                        status="error",
                        id=f"tool-{call_id}",
                    ),
                )
                completed.add(call_id)
