"""Task 输入接收与消费：inbox 到 context 的转移在同一事务内提交。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from openai.types.chat import ChatCompletionContentPartParam, ChatCompletionUserMessageParam

from app.config.logging.logger import log
from app.core.context.context_entry import ContextEntry
from app.models.conversation_input import ConversationInput, QueuedTaskInput, UserInputPresentation
from app.models.conversation_run_command import ConversationRunCommand
from app.models.conversation_run_options import ReasoningEffort
from app.models.message_metadata import UserMessageMetadata
from app.service.conversation_run.conversation_input_preparation_service import (
    ConversationInputPreparationService,
)
from app.storage.crud.task_input_crud import TaskInputCrud
from app.storage.store_engines import main_session_factory

if TYPE_CHECKING:
    from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager


def input_message_id(input_id: int) -> str:
    """inbox 行 id 渲染为该条用户消息的稳定身份。

    inbox 行不再另存 UUID：队列投影、编辑定位与消费进 context 后的消息 id 都取自同一行 id，
    因此同一条消息在「排队中」与「已消费」两个阶段共享身份，前端消息不会因消费而换 id。
    """

    return str(input_id)


@dataclass(frozen=True)
class PendingTaskInput:
    """队列读取结果保留稳定消息身份，载荷已在读取边界校验。"""

    message_id: str
    payload: QueuedTaskInput


class TaskInputService:
    def __init__(self) -> None:
        self._inputs = TaskInputCrud()

    def enqueue(
        self,
        task_id: int,
        command: ConversationRunCommand,
        model_config_id: int,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> str:
        """调用方持 Task 操作闸门；校验及附件解析复用既有领域服务。

        返回该条输入的消息身份（见 :func:`input_message_id`）。
        """
        payload = ConversationInputPreparationService().prepare_input(
            task_id, command, model_config_id, reasoning_effort
        )
        with main_session_factory().begin() as session:
            message_id = input_message_id(self._inputs.append(session, task_id, payload))
        log.info(
            "task_input_enqueued",
            extra={
                "msg": "用户输入已持久化，等待模型消费",
                "data": {"task_id": task_id, "message_id": message_id},
            },
        )
        return message_id

    def edit_latest(
        self,
        task_id: int,
        run_id: int,
        message_id: str,
        command: ConversationRunCommand,
        model_config_id: int,
        reasoning_effort: ReasoningEffort | None,
    ) -> bool:
        """调用方持 Task 闸门。队尾输入可原地编辑；已消费消息只在旧驱动释放后截断重跑。

        截断 context 与重置 Run 同一事务提交。外部文件、工具副作用不回滚。
        返回是否需要启动执行器；编辑队列不会唤醒暂停的 Run。
        """
        from app.models.conversation_task_context import ConversationTaskContextRecord
        from app.storage.crud.conversation_run_crud import ConversationRunCrud
        from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        runs = ConversationRunCrud()
        contexts = ConversationTaskContextCrud()
        with main_session_factory().begin() as session:
            run = runs.get_in_session(session, run_id)
            if run.task_id != task_id or run.model_config_id != model_config_id:
                raise ValueError("编辑目标或模型与当前 Run 不符")
            pending = self._inputs.list_pending(session, task_id)
            if pending:
                if input_message_id(pending[-1].id) != message_id:
                    raise ValueError("只能编辑最新用户消息")
                payload = self._prepare_edit_payload(
                    task_id,
                    command,
                    model_config_id,
                    reasoning_effort,
                    QueuedTaskInput.model_validate(pending[-1].payload).message,
                )
                self._inputs.replace(session, pending[-1], payload)
                return False
            if run.status in {"pending", "running", "waiting_for_input"}:
                raise ValueError("请先取消执行，再编辑已消费的消息")
            rows = contexts.get(task_id, include_in_context=False, session=session)
            users = [r for r in rows if r.message["role"] == "user" and r.project_to_transport]
            if not users or users[-1].message_id != message_id or users[-1].run_id != run_id:
                raise ValueError("只能编辑最新用户消息")
            target = users[-1]
            if (
                not isinstance(target.metadata, UserMessageMetadata)
                or target.metadata.presentation is None
            ):
                raise ValueError("编辑目标不是用户提交的消息")
            payload = self._prepare_edit_payload(
                task_id,
                command,
                model_config_id,
                reasoning_effort,
                target.metadata.presentation,
            )
            contexts.delete_from_sequence(task_id, target.sequence, session)
            contexts.create(
                ConversationTaskContextRecord(
                    task_id=task_id,
                    run_id=run_id,
                    message=user_message(payload.message),
                    message_id=message_id,
                    metadata=UserMessageMetadata(presentation=payload.message.presentation()),
                    sequence=target.sequence,
                    include_in_context=True,
                ),
                session=session,
            )
            from app.service.depends import get_conversation_run_state_service

            get_conversation_run_state_service().reset_for_edit(run_id, session)
        task_runtime_spaces.get_or_create(task_id).unload_context_manager()
        log.info(
            "task_input_edited",
            extra={
                "msg": "从用户消息处截断并重跑",
                "data": {"task_id": task_id, "run_id": run_id, "message_id": message_id},
            },
        )
        return True

    @staticmethod
    def _prepare_edit_payload(
        task_id: int,
        command: ConversationRunCommand,
        model_config_id: int,
        reasoning_effort: ReasoningEffort | None,
        previous: UserInputPresentation,
    ) -> QueuedTaskInput:
        """编辑可复用目标消息的附件路径；不从 Run 首条消息猜测附件归属。"""
        from dataclasses import replace

        from app.models.conversation_run_attachment_input import ConversationRunAttachmentInput

        attachments = {
            a["id"]: ConversationRunAttachmentInput(
                id=a["id"], name=a["name"], content_type=a["content_type"], path=a["path"]
            )
            for a in previous.attachments
        }
        for attachment in command.attachments:
            if attachment.path is not None or attachment.id not in attachments:
                attachments[attachment.id] = attachment
        return ConversationInputPreparationService().prepare_input(
            task_id,
            replace(command, attachments=list(attachments.values())),
            model_config_id,
            reasoning_effort,
        )

    def pending(self, task_id: int) -> list[PendingTaskInput]:
        with main_session_factory()() as session:
            return [
                PendingTaskInput(
                    input_message_id(row.id), QueuedTaskInput.model_validate(row.payload)
                )
                for row in self._inputs.list_pending(session, task_id)
            ]

    def consume(self, task_id: int, run_id: int, sequence: int) -> list[ContextEntry]:
        """模型边界一次领取当前批次；序号仍由唯一 RuntimeMessageContextManager 提供。"""
        from app.service.depends import get_conversation_task_context_service

        context = get_conversation_task_context_service()
        entries: list[ContextEntry] = []
        with main_session_factory().begin() as session:
            rows = self._inputs.list_pending(session, task_id)
            for row in rows:
                message_input = QueuedTaskInput.model_validate(row.payload).message
                message = user_message(message_input)
                entry = context.append(
                    task_id,
                    run_id,
                    message,
                    sequence + len(entries),
                    session=session,
                    message_id=input_message_id(row.id),
                    metadata=UserMessageMetadata(presentation=message_input.presentation()),
                )
                entries.append(entry)
            self._inputs.delete(session, [row.id for row in rows])
        if entries:
            log.info(
                "task_inputs_consumed",
                extra={
                    "msg": "用户输入已归属当前 Run",
                    "data": {
                        "task_id": task_id,
                        "run_id": run_id,
                        "message_ids": [e.message_id for e in entries],
                    },
                },
            )
        return entries

    def consume_into_context(self, context: RuntimeMessageContextManager, run_id: int) -> None:
        """Task 闸门内转移 inbox，提交后更新 working copy 并投影；序号由 context owner 提供。"""
        from app.service.depends import get_conversation_task_state_service
        from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

        context.close_unclosed_tool_calls()
        with task_runtime_spaces.get_or_create(context.current_task_id).operation():
            entries = self.consume(context.current_task_id, run_id, context.next_sequence)
            context.accept_committed_inputs(entries)
        if entries:
            get_conversation_task_state_service().project_consumed_inputs(
                context.current_task_id, run_id, entries
            )


def user_message(message: ConversationInput) -> ChatCompletionUserMessageParam:
    """正文保持 SDK 格式；图片使用本地 locator，发请求前解析，不落盘 base64。"""
    if not message.image_paths:
        return {"role": "user", "content": message.input_text}
    content: list[ChatCompletionContentPartParam] = []
    if message.input_text:
        content.append({"type": "text", "text": message.input_text})
    content.extend(
        {"type": "image_url", "image_url": {"url": f"cosir-attachment://{path}"}}
        for path in message.image_paths
    )
    return {"role": "user", "content": content}
