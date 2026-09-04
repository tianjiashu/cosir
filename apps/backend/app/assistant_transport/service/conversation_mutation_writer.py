"""Conversation snapshot/context 的领域 mutation writer。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast

from langchain_core.messages import AIMessage

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationStateMutation,
    ConversationTaskSnapshotService,
    find_assistant_message_index,
)
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateMessage,
    ConversationStateSnapshot,
    empty_snapshot,
)
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.storage.crud.conversation_run_crud import ConversationRunCrud


@dataclass(frozen=True)
class MessageHandle:
    """snapshot 中一条消息的稳定句柄。"""

    id: str
    task_id: int
    run_id: int | None


class ConversationMutationWriter:
    """只通过 snapshot/context owner 写入 Conversation 语义事实。"""

    def __init__(
        self,
        snapshots: ConversationTaskSnapshotService | None = None,
        run_crud: ConversationRunCrud | None = None,
    ) -> None:
        """初始化 snapshot owner 与 run 数据访问。

        参数:
            snapshots: 可选 snapshot owner；None 时使用默认实例。
            run_crud: 可选 run 纯 CRUD；None 时使用默认实例。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self._snapshots = snapshots or ConversationTaskSnapshotService()
        self._run_crud = run_crud or ConversationRunCrud()


    def append_text(self, task_id: int, message_id: str, text: str) -> object:
        """向指定 snapshot 消息的 text part 追加文本。"""

        if not text:
            raise ValueError("text must not be empty")
        state = self._snapshots.load(task_id) or empty_snapshot()
        index, part_index = _find_message_part(state, message_id, "text")
        self._snapshots.mutate(
            task_id,
            [
                ConversationStateMutation(
                    "append-text", ("messages", index, "parts", part_index, "text"), text
                )
            ],
        )
        return SimpleNamespace(id=message_id)

    def create_run_baseline(self, task_id: int, run_id: int, input_text: str) -> None:
        """为 Run 创建 snapshot user/assistant 消息基线。"""

        state = self._snapshots.load(task_id) or empty_snapshot()
        if any(message.get("runId") == run_id for message in state["messages"]):
            return
        created_at = datetime.now(UTC).isoformat()
        offset = len(state["messages"])
        self._snapshots.mutate(
            task_id,
            [
                ConversationStateMutation(
                    "set", ("messages", offset), _message(
                        f"user-{run_id}", run_id, "user", "completed", created_at, input_text, "completed"
                    )
                ),
                ConversationStateMutation(
                    "set", ("messages", offset + 1), _message(
                        f"assistant-{run_id}", run_id, "assistant", "running", created_at, "", "running"
                    )
                ),
                ConversationStateMutation("set", ("run", "runId"), run_id),
                ConversationStateMutation("set", ("run", "status"), "pending"),
            ],
        )

    def append_assistant_text_for_run(self, task_id: int, run_id: int, text: str) -> object:
        """向当前 Run 的 assistant text part 追加 chunk。"""

        message_id = self._message_id_for_run(task_id, run_id)
        return self.append_text(task_id, message_id, text)

    def append_assistant_part_for_run(
        self, task_id: int, run_id: int, part_type: str, text: str
    ) -> object:
        """向当前 Run 的 assistant reasoning/text part 追加 chunk。"""

        if part_type not in {"text", "reasoning"}:
            raise ValueError("unsupported assistant part")
        if not text:
            raise ValueError("text must not be empty")
        state = self._snapshots.load(task_id) or empty_snapshot()
        index, part_index = _find_message_part(
            state, self._message_id_for_state(state, run_id), part_type
        )
        kind: Literal["append-text"] = "append-text"
        self._snapshots.mutate(
            task_id,
            [ConversationStateMutation(kind, ("messages", index, "parts", part_index, "text"), text)],
        )
        return SimpleNamespace(id=part_index)

    def create_ai_message_with_tool_calls(
        self,
        task_id: int,
        run_id: int,
        message: AIMessage,
        tool_calls: Sequence[tuple[str, str, object]],
    ) -> None:
        """写入完整 AIMessage 对应的 snapshot tool-call parts。

        参数:
            task_id: 目标 Task 标识。
            run_id: 产生消息的 Conversation Run 标识。
            message: 已组装完成的 LangChain ``AIMessage``。
            tool_calls: 按 ``(call_id, tool_name, arguments)`` 排列的工具调用。

        返回:
            无。

        异常:
            KeyError: assistant baseline 不存在。
            ValueError: 工具调用标识为空或参数结构非法。
            sqlalchemy.exc.SQLAlchemyError: snapshot/context 写入失败。

        副作用:
            snapshot tool-call parts 独立写入；AIMessage 的 context 持久化由 context owner
            负责。
        """

        state = self._snapshots.load(task_id) or empty_snapshot()
        assistant_id = self._message_id_for_state(state, run_id)
        message_index, assistant = _find_message(state, assistant_id)
        existing_call_ids = {
            str(part.get("toolCallId"))
            for part in assistant["parts"]
            if isinstance(part, dict) and part.get("type") == "tool-call"
        }
        mutations: list[ConversationStateMutation] = []
        next_part_index = len(assistant["parts"])
        for call_id, tool_name, arguments in tool_calls:
            if not call_id:
                raise ValueError("tool_call_id must not be empty")
            if call_id in existing_call_ids:
                continue
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            mutations.append(
                ConversationStateMutation(
                    "set",
                    ("messages", message_index, "parts", next_part_index),
                    {
                        "type": "tool-call",
                        "toolCallId": call_id,
                        "toolName": tool_name,
                        "status": "pending",
                        "args": arguments,
                        "result": None,
                        "error": None,
                        "isError": False,
                        "approvalRequestId": None,
                    },
                )
            )
            existing_call_ids.add(call_id)
            next_part_index += 1
        if mutations:
            self._snapshots.mutate(task_id, mutations)

    def create_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: object,
        run_id: int | None = None,
        message_id: str | None = None,
        part_id: str | None = None,
    ) -> tuple[object, object]:
        """在 assistant snapshot 中创建 pending tool-call part。"""

        if not tool_call_id:
            raise ValueError("tool_call_id must not be empty")
        state = self._snapshots.load(task_id) or empty_snapshot()
        assistant_id = message_id or self._message_id_for_state(state, run_id)
        index, _ = _find_message(state, assistant_id)
        if any(
            isinstance(part, dict)
            and part.get("type") == "tool-call"
            and part.get("toolCallId") == tool_call_id
            for part in state["messages"][index]["parts"]
        ):
            return SimpleNamespace(tool_call_id=tool_call_id), SimpleNamespace(id=part_id)
        part_index = len(state["messages"][index]["parts"])
        part = {
            "type": "tool-call",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "status": "pending",
            "args": arguments if isinstance(arguments, dict) else {},
            "result": None,
            "error": None,
            "isError": False,
            "approvalRequestId": None,
        }
        self._snapshots.mutate(
            task_id,
            [ConversationStateMutation("set", ("messages", index, "parts", part_index), part)],
        )
        return SimpleNamespace(
            tool_call_id=tool_call_id, part_id=f"{assistant_id}-part-{part_index}"
        ), SimpleNamespace(id=f"{assistant_id}-part-{part_index}")

    def transition_tool_call(
        self, task_id: int, tool_call_id: str, status: str, run_id: int | None = None
    ) -> object | None:
        """校验并迁移工具调用状态。"""

        if status not in {"pending", "running", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported tool status")
        state = self._snapshots.load(task_id) or empty_snapshot()
        index, part_index = _find_tool(state, tool_call_id)
        current = state["messages"][index]["parts"][part_index]["status"]
        allowed = {
            "pending": {"pending", "running", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        if status not in allowed[current]:
            raise ValueError(f"invalid tool transition {current} -> {status}")
        self._snapshots.mutate(
            task_id,
            [ConversationStateMutation("set", ("messages", index, "parts", part_index, "status"), status)],
        )
        return SimpleNamespace(tool_call_id=tool_call_id, status=status)

    def complete_tool_call_by_external_id(
        self,
        task_id: int,
        tool_call_id: str,
        result: object,
        *,
        status: str = "completed",
        error_text: str | None = None,
        run_id: int | None = None,
    ) -> object | None:
        """独立写入 tool result/error 的 snapshot 状态。"""

        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("tool completion requires a terminal status")
        state = self._snapshots.load(task_id) or empty_snapshot()
        index, part_index = _find_tool(state, tool_call_id)
        current = state["messages"][index]["parts"][part_index]["status"]
        if current in {"completed", "failed", "cancelled"}:
            if current != status:
                raise ValueError(f"terminal tool status conflict: {current} vs {status}")
            return SimpleNamespace(tool_call_id=tool_call_id, status=current)
        self._snapshots.mutate(
            task_id,
            [
                ConversationStateMutation(
                    "set", ("messages", index, "parts", part_index, "status"), status
                ),
                ConversationStateMutation(
                    "set", ("messages", index, "parts", part_index, "result"),
                    result if status != "failed" else None,
                ),
                ConversationStateMutation(
                    "set", ("messages", index, "parts", part_index, "error"),
                    error_text if status == "failed" else None,
                ),
                ConversationStateMutation(
                    "set", ("messages", index, "parts", part_index, "isError"),
                    status == "failed",
                ),
            ],
        )
        return SimpleNamespace(tool_call_id=tool_call_id, status=status)

    def settle_open_tool_calls(self, run_id: int, status: str, reason: str) -> None:
        """将指定 Run 遗留的 pending/running tools 收束为 failed/cancelled。"""

        task_id = self._task_id_for_run(run_id)
        state = self._snapshots.load(task_id) or empty_snapshot()
        ids = [
            str(cast(dict[str, object], part)["toolCallId"])
            for message in state["messages"]
            if message.get("runId") == run_id
            for part in message["parts"]
            if isinstance(part, dict)
            and part.get("type") == "tool-call"
            and part.get("status") in {"pending", "running"}
        ]
        target = "cancelled" if status == "cancelled" else "failed"
        for tool_call_id in ids:
            self.complete_tool_call_by_external_id(
                task_id, tool_call_id, None, status=target, error_text=reason, run_id=run_id
            )

    def recover_interrupted_run(self, run_id: int) -> object | None:
        """收束崩溃遗留 Run、snapshot tools 与 context tool messages。

        Run 终态经 run CRUD 独立事务原子更新，snapshot 收敛经 snapshot owner 单独提交。

        参数:
            run_id: 后端重启时待恢复的 Conversation Run 标识。

        返回:
            首次收束时返回包含 ``id`` 与 ``status`` 的轻量记录；Run 已不是 active 时返回
            ``None``，使恢复过程天然幂等。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 Run 终态更新或 snapshot 收敛写入失败。

        副作用:
            将 active Run 经 run CRUD 在独立事务内原子标为 failed；snapshot 中未闭合
            tool-call 的收敛经 snapshot owner 单独提交；context 的恢复补偿由 context owner 负责。
        """

        task_id = self._task_id_for_run(run_id)
        record = self._run_crud.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            "backend_restarted",
        )
        if record is None:
            return None

        state = self._snapshots.load(task_id) or empty_snapshot()
        mutations: list[ConversationStateMutation] = []
        assistant_index = find_assistant_message_index(state, run_id)
        if assistant_index is not None:
            for part_index, part in enumerate(state["messages"][assistant_index]["parts"]):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool-call"
                    and part.get("status") in {"pending", "running"}
                ):
                    mutations.extend(
                        [
                            ConversationStateMutation(
                                "set",
                                ("messages", assistant_index, "parts", part_index, "status"),
                                "failed",
                            ),
                            ConversationStateMutation(
                                "set",
                                ("messages", assistant_index, "parts", part_index, "error"),
                                "execution_interrupted",
                            ),
                            ConversationStateMutation(
                                "set",
                                ("messages", assistant_index, "parts", part_index, "isError"),
                                True,
                            ),
                        ]
                    )
            mutations.extend(
                [
                    ConversationStateMutation(
                        "set", ("messages", assistant_index, "status"), "failed"
                    ),
                    ConversationStateMutation(
                        "set", ("messages", assistant_index, "endReason"), "backend_restarted"
                    ),
                ]
            )
        mutations.append(ConversationStateMutation("set", ("run", "status"), "failed"))
        self._snapshots.mutate(task_id, mutations)

        return SimpleNamespace(id=run_id, status=ConversationRunStatus.FAILED.value)

    def settle_run(self, run_id: int, status: str, end_reason: str | None = None) -> object | None:
        """先更新 Run 终态，再尽力收敛 assistant snapshot 展示状态。"""

        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid terminal run status")
        task_id = self._task_id_for_run(run_id)
        record = self._run_crud.update_status_if_in(
            run_id,
            status,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
        )
        if record is None:
            return None
        state = self._snapshots.load(task_id) or empty_snapshot()
        index = find_assistant_message_index(state, run_id)
        mutations = [ConversationStateMutation("set", ("run", "status"), status)]
        if index is not None:
            mutations.extend(
                [
                    ConversationStateMutation("set", ("messages", index, "status"), status),
                    ConversationStateMutation("set", ("messages", index, "endReason"), end_reason),
                ]
            )
        self._snapshots.mutate(task_id, mutations)
        return SimpleNamespace(id=run_id, status=status)

    def complete_run_with_message(
        self, run_id: int, message: AIMessage, end_reason: str | None = None
    ) -> object | None:
        """完成 Run，并独立收敛其 snapshot 展示状态。

        参数:
            run_id: 待完成的 Conversation Run 标识。
            message: 模型本轮生成的完整 ``AIMessage``，不得传入增量 chunk。
            end_reason: 可选的终态原因。

        返回:
            成功完成时返回包含 ``id`` 与 ``status`` 的轻量记录；run 已经进入其他终态时返回
            ``None``。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果 snapshot、context 或 run 写入失败。

        副作用:
            将 Run 经 run CRUD 在独立事务内原子置为 completed；snapshot 展示状态（含 assistant
            消息 status/endReason 与 run 状态）经 snapshot owner 单独提交。
        """

        task_id = self._task_id_for_run(run_id)
        record = self._run_crud.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
        )
        if record is None:
            return None
        state = self._snapshots.load(task_id) or empty_snapshot()
        index = find_assistant_message_index(state, run_id)
        if index is None:
            raise KeyError(f"assistant message for run {run_id} not found")
        self._snapshots.mutate(
            task_id,
            [
                ConversationStateMutation("set", ("messages", index, "status"), "completed"),
                ConversationStateMutation("set", ("messages", index, "endReason"), end_reason),
                ConversationStateMutation("set", ("run", "status"), "completed"),
            ],
        )
        return SimpleNamespace(id=run_id, status=ConversationRunStatus.COMPLETED.value)

    def cancel_run(self, run_id: int, end_reason: str = "user_cancelled") -> object | None:
        """将 active Run 原子收束为 cancelled。"""

        return self.settle_run(run_id, "cancelled", end_reason)

    def _task_id_for_run(self, run_id: int) -> int:
        """读取 Run 所属 Task。

        参数:
            run_id: 待查询的 Conversation Run 标识。

        返回:
            该 run 所属 task 的整数 id。

        异常:
            KeyError: 如果指定 run 不存在。

        副作用:
            经 run CRUD 打开一次主库只读 session。
        """

        return self._run_crud.get(run_id).task_id

    def _message_id_for_run(self, task_id: int, run_id: int) -> str:
        """返回 Run assistant snapshot 消息 id。"""

        state = self._snapshots.load(task_id) or empty_snapshot()
        return self._message_id_for_state(state, run_id)

    @staticmethod
    def _message_id_for_state(state: ConversationStateSnapshot, run_id: int | None) -> str:
        """从 snapshot 中查找指定 Run 的 assistant 消息。"""

        for message in state["messages"]:
            if message.get("runId") == run_id and message.get("role") == "assistant":
                return str(message["id"])
        raise KeyError(f"assistant message for run {run_id} not found")


def _message(
    message_id: str,
    run_id: int | None,
    role: Literal["user", "assistant"],
    status: str,
    created_at: str,
    text: str,
    part_status: Literal["running", "completed"],
) -> ConversationStateMessage:
    """构造 snapshot user/assistant 消息。"""

    return {
        "id": message_id,
        "runId": run_id,
        "role": role,
        "status": status,
        "endReason": None,
        "createdAt": created_at,
        "parts": [{"type": "text", "text": text, "status": part_status}],
    }


def _find_message(
    state: ConversationStateSnapshot, message_id: str
) -> tuple[int, ConversationStateMessage]:
    """查找消息索引。"""

    for index, message in enumerate(state["messages"]):
        if message["id"] == message_id:
            return index, message
    raise KeyError(message_id)


def _find_message_part(
    state: ConversationStateSnapshot, message_id: str, part_type: str
) -> tuple[int, int]:
    """查找消息中的 part 索引。"""

    index, message = _find_message(state, message_id)
    for part_index, part in enumerate(message["parts"]):
        if isinstance(part, dict) and part.get("type") == part_type:
            return index, part_index
    raise KeyError(f"{part_type} part for {message_id} not found")


def _find_tool(state: ConversationStateSnapshot, tool_call_id: str) -> tuple[int, int]:
    """按 Task 内唯一 toolCallId 查找 tool part。"""

    for index, message in enumerate(state["messages"]):
        for part_index, part in enumerate(message["parts"]):
            if (
                isinstance(part, dict)
                and part.get("type") == "tool-call"
                and part.get("toolCallId") == tool_call_id
            ):
                return index, part_index
    raise KeyError(tool_call_id)
