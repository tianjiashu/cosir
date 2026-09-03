"""Canonical conversation adapter for the runtime context message port.

``RuntimeContextManager`` deliberately depends on the small
``RuntimeMessageStore`` protocol instead of importing service or storage code.  This
adapter is the service-side implementation of that port.  New runtime messages are
written through ``ConversationMutationWriter``; the legacy ``turn_messages`` CRUD
is not used by this adapter.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Collection
from typing import Any, Protocol

from sqlalchemy import func, or_, select

from app.core.context.context_entry import ContextEntry
from app.core.context.runtime_message_store import RuntimeMessageStore
from app.models import RuntimeMessage
from app.assistant_transport.service.conversation_mutation_writer import ConversationMutationWriter
from app.storage.model.conversation_message_model import ConversationMessageModel
from app.storage.model.conversation_message_part_model import ConversationMessagePartModel
from app.storage.model.conversation_run_model import ConversationRunModel
from app.storage.model.conversation_tool_call_model import ConversationToolCallModel
from app.storage.store_engines import main_session_factory


class ConversationMessageWriter(Protocol):
    """Runtime context 所需的最小 canonical message 写入端口。"""

    def create_message(
        self,
        task_id: int,
        role: str,
        run_id: int | None = None,
        status: str = "complete",
        end_reason: str | None = None,
        text: str | None = None,
        part_type: str = "text",
    ) -> object:
        """创建一条 canonical message 及其首个 part。"""

    def create_tool_message(
        self, task_id: int, run_id: int, tool_call_id: str, text: str
    ) -> object:
        """创建带显式 tool_call_id 关联的模型工具消息。"""

    def append_text_for_run(
        self,
        task_id: int,
        run_id: int,
        text: str,
    ) -> object:
        """向指定运行的 assistant 文本 part 追加内容。"""

    def create_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: object,
        run_id: int | None = None,
        message_id: int | None = None,
        part_id: int | None = None,
    ) -> object:
        """持久化一条结构化工具调用事实。"""

    def clear_run(self, run_id: int) -> None:
        """清理指定运行关联的运行期 canonical conversation 事实。"""


class ConversationRunMessageStore(RuntimeMessageStore):
    """将运行时消息端口适配到 canonical conversation facts。

    适配器保留旧构造函数的第一个参数以避免改动当前 RuntimeOperations 的装配，
    但该参数不再参与消息读写。运行链路预先创建的 user/assistant 占位消息会被识别
    并复用；工具调用的结构化事实仍由 RuntimeOperations 通过同一个 Writer 写入，
    这里只持久化模型上下文所需的 tool message。

    fresh run 的清理由 ``ConversationMutationWriter.clear_run`` 统一完成：在同一 SQLite
    写事务中删除运行期 message、part 和 tool-call 行，同时保留运行链路预创建的
    user/assistant 占位、重置 assistant part，并同步裁剪任务快照。
    """

    def __init__(
        self,
        _legacy_conversation_run_state_service: object | None = None,
        mutation_writer: ConversationMessageWriter | None = None,
    ) -> None:
        """初始化 canonical message 适配器。

        参数:
            _legacy_conversation_run_state_service: 迁移兼容参数；仅保留调用签名，
                不再访问其消息方法。
            mutation_writer: 可注入的最小 canonical writer；缺省使用生产 Writer。

        返回:
            无。

        异常:
            RuntimeError: 未初始化 storage 且未能构造默认 Writer 时抛出。

        副作用:
            保存 Writer 引用；不访问旧 ``turn_messages`` 表。
        """
        del _legacy_conversation_run_state_service
        self._mutation_writer = mutation_writer or ConversationMutationWriter()

    def append(
        self,
        run_id: int,
        message: RuntimeMessage,
        sequence: int,
        include_in_context: bool = True,
    ) -> None:
        """将一条运行时消息写入 canonical conversation。

        参数:
            run_id: 目标 Conversation Run 标识。
            message: 单条模型无关的运行时消息。
            sequence: 旧端口要求的轮内序号；canonical Writer 使用 task 级序号，故不消费。
            include_in_context: 是否进入内存上下文；持久化由 RuntimeContextManager 控制，
                canonical message 本身不重复保存该运行时投影标志。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: canonical 写入失败。
            ValueError: message role 或 Writer 参数非法。

        副作用:
            经 ``ConversationMutationWriter.create_message`` 创建 canonical message；对
            已由运行编排层创建的 user/assistant 占位消息不重复创建。
        """
        del sequence, include_in_context
        task_id = self._task_id_for_run(run_id)
        if message.role == "assistant" and self._message_exists(run_id, message.role):
            if message.content_text:
                self._mutation_writer.append_text_for_run(
                    task_id, run_id, message.content_text
                )
            raw_tool_calls = (message.metadata or {}).get("tool_calls")
            if raw_tool_calls:
                try:
                    tool_calls = json.loads(raw_tool_calls)
                except (TypeError, ValueError):
                    tool_calls = []
                for call in tool_calls if isinstance(tool_calls, list) else []:
                    if not isinstance(call, dict) or not call.get("id"):
                        continue
                    self._mutation_writer.create_tool_call(
                        task_id,
                        str(call["id"]),
                        str(call.get("name") or "unknown_tool"),
                        call.get("args", {}),
                        run_id=run_id,
                    )
            return
        if message.role == "user" and self._message_exists(run_id, message.role):
            return
        if message.role == "tool":
            tool_call_id = str((message.metadata or {}).get("tool_call_id") or "")
            if tool_call_id:
                self._mutation_writer.create_tool_message(
                    task_id, run_id, tool_call_id, message.content_text
                )
                return
        self._mutation_writer.create_message(
            task_id, message.role, run_id=run_id, text=message.content_text
        )

    def clear(self, run_id: int) -> None:
        """清理指定 Conversation Run 的 canonical conversation 事实。

        参数:
            run_id: 待清理的 Conversation Run 标识。

        返回:
            无。

        异常:
            KeyError: Conversation Run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 删除或 canonical fact 写入失败。

        副作用:
            在一个 immediate SQLite 写事务中删除该 run 的运行期 canonical messages、
            parts 和 tool calls，保留并重置 user/assistant 占位。
        """
        self._mutation_writer.clear_run(run_id)

    def build_for_task(
        self,
        task_id: int,
        excluded_run_ids: Collection[int] | None = None,
    ) -> list[ContextEntry]:
        """按 canonical task message sequence 构建历史上下文。

        参数:
            task_id: 目标 task 标识。
            excluded_run_ids: 不应进入本次上下文的 Conversation Run 标识集合。

        返回:
            按 canonical task sequence 排序的 ``ContextEntry`` 列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: canonical 事实读取失败。

        副作用:
            打开一次只读 session；不访问旧消息表。
        """
        return self._build_entries(task_id=task_id, excluded_run_ids=excluded_run_ids)

    def build_for_run(self, run_id: int) -> list[ContextEntry]:
        """按 canonical facts 恢复一个 Conversation Run 的运行时上下文。

        参数:
            run_id: 目标 Conversation Run 标识。

        返回:
            按 canonical message sequence 排序的上下文条目。

        异常:
            sqlalchemy.exc.SQLAlchemyError: canonical 事实读取失败。

        副作用:
            打开一次只读 session；不访问旧消息表。
        """
        return self._build_entries(run_id=run_id)

    def next_run_sequence(self, run_id: int) -> int:
        """返回 Conversation Run 是否已有 canonical message 的兼容性快照。

        canonical Writer 使用 task 级序号，RuntimeContextManager 的旧轮内序号不再参与
        持久化；该返回值仅保留 RuntimeMessageStore 协议形状，避免恢复路径改变接口。
        """
        session_factory = main_session_factory()
        with session_factory() as session:
            count = session.scalar(
                select(func.count())
                .select_from(ConversationMessageModel)
                .where(ConversationMessageModel.run_id == run_id)
            )
            return int(count or 0)

    def _task_id_for_run(self, run_id: int) -> int:
        """读取 Conversation Run 所属 task，供 canonical Writer 做聚合根校验。"""
        session_factory = main_session_factory()
        with session_factory() as session:
            run = session.get(ConversationRunModel, run_id)
            if run is None:
                raise KeyError(run_id)
            return run.task_id

    def _message_exists(self, run_id: int, role: str) -> bool:
        """判断指定 Conversation Run 是否已有对应角色的 canonical message。"""
        session_factory = main_session_factory()
        with session_factory() as session:
            return (
                session.scalar(
                    select(ConversationMessageModel.id)
                    .where(
                        ConversationMessageModel.run_id == run_id,
                        ConversationMessageModel.role == role,
                    )
                    .limit(1)
                )
                is not None
            )

    def _build_entries(
        self,
        *,
        task_id: int | None = None,
        run_id: int | None = None,
        excluded_run_ids: Collection[int] | None = None,
    ) -> list[ContextEntry]:
        """读取 canonical message/part/tool-call facts 并重建运行时消息。"""
        if task_id is None and run_id is None:
            raise ValueError("task_id or run_id is required")

        session_factory = main_session_factory()
        with session_factory() as session:
            conditions: list[Any] = []
            if task_id is not None:
                conditions.append(ConversationMessageModel.task_id == task_id)
            if run_id is not None:
                conditions.append(ConversationMessageModel.run_id == run_id)
            if excluded_run_ids:
                excluded = set(excluded_run_ids)
                conditions.append(
                    or_(
                        ConversationMessageModel.run_id.is_(None),
                        ConversationMessageModel.run_id.not_in(excluded),
                    )
                )
            messages = (
                session.execute(
                    select(ConversationMessageModel)
                    .where(*conditions)
                    .order_by(ConversationMessageModel.sequence.asc())
                )
                .scalars()
                .all()
            )
            if not messages:
                return []

            message_ids = [message.id for message in messages]
            parts = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .where(ConversationMessagePartModel.message_id.in_(message_ids))
                    .order_by(
                        ConversationMessagePartModel.message_id.asc(),
                        ConversationMessagePartModel.sequence.asc(),
                    )
                )
                .scalars()
                .all()
            )
            calls = (
                session.execute(
                    select(ConversationToolCallModel)
                    .where(ConversationToolCallModel.message_id.in_(message_ids))
                    .order_by(
                        ConversationToolCallModel.message_id.asc(),
                        ConversationToolCallModel.created_at.asc(),
                        ConversationToolCallModel.id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            parts_by_message: dict[int, list[ConversationMessagePartModel]] = defaultdict(list)
            for part in parts:
                parts_by_message[part.message_id].append(part)
            calls_by_message: dict[int, list[ConversationToolCallModel]] = defaultdict(list)
            for call in calls:
                if call.message_id is not None:
                    calls_by_message[call.message_id].append(call)

            entries: list[ContextEntry] = []
            for message in messages:
                message_run_id = message.run_id
                # 运行编排预先创建的 assistant running 空消息只是 canonical UI
                # 占位，不是模型上下文事实；把它送入 OpenAI 兼容端点会形成
                # assistant.content=null / reasoning_content=null 等非法请求。
                has_visible_content = any(
                    part.part_type in {"text", "reasoning"} and bool(part.text)
                    for part in parts_by_message[message.id]
                )
                if (
                    message.role == "assistant"
                    and message.status in {"pending", "running"}
                    and not has_visible_content
                    and not calls_by_message[message.id]
                ):
                    continue
                entries.append(
                    ContextEntry(
                        message=self._to_runtime_message(
                            message,
                            parts_by_message[message.id],
                            calls_by_message[message.id],
                        ),
                        run_id=message_run_id,
                    )
                )
            return entries

    @staticmethod
    def _to_runtime_message(
        message: ConversationMessageModel,
        parts: list[ConversationMessagePartModel],
        calls: list[ConversationToolCallModel],
    ) -> RuntimeMessage:
        """将 canonical message、parts 和工具调用事实转换为 RuntimeMessage。"""
        text = "".join(part.text or "" for part in parts if part.part_type == "text")
        metadata: dict[str, Any] = {}
        reasoning = "".join(part.text or "" for part in parts if part.part_type == "reasoning")
        if reasoning:
            metadata["reasoning_content"] = reasoning
        if message.role == "assistant" and calls:
            metadata["tool_calls"] = json.dumps(
                [
                    {
                        "name": call.tool_name,
                        "args": _json_object(call.args_json),
                        "id": call.tool_call_id,
                    }
                    for call in calls
                ],
                ensure_ascii=False,
            )
        if message.role == "tool":
            for part in parts:
                if not part.data_json:
                    continue
                try:
                    data = json.loads(part.data_json)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("toolCallId"):
                    metadata["tool_call_id"] = str(data["toolCallId"])
                    break
        return RuntimeMessage(role=message.role, content_text=text, metadata=metadata)


def _json_object(raw: str) -> object:
    """安全解析 canonical tool-call 参数；非法数据保留原始文本。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw
