"""Conversation snapshot/context 的领域 mutation writer。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

from langchain_core.messages import AIMessage

from app.assistant_transport.service.conversation_task_snapshot_service import (
    ConversationTaskSnapshotService,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_message import ConversationStateMessage
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.models.enums.conversation_run_status import ConversationRunStatus
from app.storage.crud.conversation_run_crud import ConversationRunCrud


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


    def _append_part_text(
        self, task_id: int, message_id: str, part_type: str, text: str
    ) -> object:
        """向指定消息的指定类型 part 追加文本，返回该 part 的位置信息。

        统一 ``append_text`` 与 ``append_assistant_part_for_run`` 的公共追加逻辑：读最新提交态、
        定位目标 part、
        构造单条 ``append-text`` mutation 并经 ``apply`` 提交。

        参数:
            task_id: 目标 Task 标识。
            message_id: 追加目标消息标识。
            part_type: 目标 part 类型（如 ``"text"`` / ``"reasoning"``）。
            text: 非空追加文本。

        返回:
            含 ``id``（消息标识）与 ``part_index``（part 索引）的命名空间。

        异常:
            ValueError: ``text`` 为空。
            KeyError: 指定消息或目标 part 不存在。

        副作用:
            经 snapshot owner 原子追加文本并提交 canonical fact；本方法依赖
            ``assistant_api`` 的 task 锁消除 task 内并发竞态，故使用 ``apply`` 而非
            ``apply_planned``；若将来 task 内出现并发写，需改回 ``apply_planned``。
        """

        if not text:
            raise ValueError("text must not be empty")
        state = self._snapshots.ensure_state_snapshot(task_id)
        index, part_index = _find_message_part(state, message_id, part_type)
        mutation = ConversationStateMutation(
            "append-text",
            ("messages", index, "parts", part_index, "text"),
            text,
        )
        self._snapshots.apply(task_id, [mutation])
        return SimpleNamespace(id=message_id, part_index=part_index)

    def append_text(self, task_id: int, message_id: str, text: str) -> object:
        """向指定 snapshot 消息的 text part 追加文本。

        参数:
            task_id: 目标 Task 标识。
            message_id: 追加目标消息标识。
            text: 非空追加文本。

        返回:
            含消息与 part 位置信息的命名空间。

        异常:
            ValueError: ``text`` 为空。

        副作用:
            经 snapshot owner 原子追加文本并提交 canonical fact。
        """

        return self._append_part_text(task_id, message_id, "text", text)

    def create_run(self, task_id: int, run_id: int) -> None:
        """幂等创建指定 Run 的 message 骨架与 run 元数据。

        仅创建 user/assistant 两条空消息与 run 状态；用户输入文本由 ``append_user_input``
        独立完成。重复进入（同 runId 的消息已存在）直接返回，保证幂等。

        参数:
            task_id: 目标 Task 标识。
            run_id: 待创建消息基线的 Conversation Run 标识。

        返回:
            无。

        异常:
            无。

        副作用:
            经 snapshot owner 原子写入 user/assistant 空消息与 run 状态；本方法依赖
            ``assistant_api`` 的 task 锁消除 task 内并发竞态，因此使用 ``apply`` 而非
            ``apply_planned``；若将来 task 内出现并发写，需改回 ``apply_planned``。
        """

        state = self._snapshots.ensure_state_snapshot(task_id)
        if any(message.get("runId") == run_id for message in state.get("messages", [])):
            return
        offset = len(state["messages"])
        mutations = [
            ConversationStateMutation(
                "set", ("messages", offset), _message(
                    f"user-{run_id}", run_id, "user", "completed", "", "completed"
                )
            ),
            ConversationStateMutation(
                "set", ("messages", offset + 1), _message(
                    f"assistant-{run_id}", run_id, "assistant", "running", "", "running"
                )
            ),
            ConversationStateMutation("set", ("run", "runId"), run_id),
            ConversationStateMutation("set", ("run", "status"), "pending"),
        ]
        self._snapshots.apply(task_id, mutations)

    def append_user_input(self, task_id: int, run_id: int, input_text: str) -> object:
        """向指定 Run 的 user 消息文本 part 追加用户输入。

        复用 ``append_text``，因此与流式 assistant 文本走同一套原子追加通道。

        参数:
            task_id: 目标 Task 标识。
            run_id: 目标 Conversation Run 标识。
            input_text: 用户输入文本（非空）。

        返回:
            含消息与 part 位置信息的命名空间。

        异常:
            ValueError: ``input_text`` 为空。
            KeyError: 指定 run 的 user 消息不存在。

        副作用:
            经 snapshot owner 原子追加用户输入文本并提交 canonical fact；与 ``create_run``
            同受 task 锁保护，不存在重复写入竞态。
        """
        message_id = None
        state = self._snapshots.ensure_state_snapshot(task_id)
        for message in state.get("messages", []):
            if message.get("runId") == run_id and message.get("role") == "user":
                message_id =  str(message["id"])
                break
        if message_id is None:
            raise KeyError(f"user message for run {run_id} not found")
        return self.append_text(task_id, message_id, input_text)

    def append_assistant_part_for_run(
        self, task_id: int, run_id: int, part_type: str, text: str
    ) -> object:
        """向当前 Run 的 assistant reasoning/text part 追加 chunk。

        参数:
            task_id: 目标 Task 标识。
            run_id: 目标 Conversation Run 标识。
            part_type: 目标 part 类型（仅 ``"text"`` 或 ``"reasoning"``）。
            text: 非空追加文本。

        返回:
            含消息与 part 位置信息的命名空间。

        异常:
            ValueError: ``part_type`` 非法或 ``text`` 为空。
            KeyError: 指定 run 的 assistant 消息或目标 part 不存在。

        副作用:
            经 snapshot owner 原子追加文本并提交 canonical fact；复用 ``_append_part_text``
            与 ``append_text`` 同一通道，依赖 task 锁消除并发竞态。
        """

        if part_type not in {"text", "reasoning"}:
            raise ValueError("unsupported assistant part")
        message_id = self._message_id_for_run(task_id, run_id)
        return self._append_part_text(task_id, message_id, part_type, text)

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

        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
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
                mutations.append(ConversationStateMutation(
                    "set", ("messages", message_index, "parts", next_part_index),
                    {"type": "tool-call", "toolCallId": call_id, "toolName": tool_name,
                     "status": "pending", "args": arguments, "result": None,
                     "error": None, "isError": False, "approvalRequestId": None},
                ))
                existing_call_ids.add(call_id)
                next_part_index += 1
            return mutations

        self._snapshots.apply_planned(task_id, plan)

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
        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            assistant_id = message_id or self._message_id_for_state(state, run_id)
            index, _ = _find_message(state, assistant_id)
            if any(isinstance(part, dict) and part.get("type") == "tool-call"
                   and part.get("toolCallId") == tool_call_id
                   for part in state["messages"][index]["parts"]):
                return []
            part_index = len(state["messages"][index]["parts"])
            part = {"type": "tool-call", "toolCallId": tool_call_id, "toolName": tool_name,
                    "status": "pending", "args": arguments if isinstance(arguments, dict) else {},
                    "result": None, "error": None, "isError": False, "approvalRequestId": None}
            return [ConversationStateMutation("set", ("messages", index, "parts", part_index), part)]

        change = self._snapshots.apply_planned(task_id, plan)
        state = change.state
        assistant_id = message_id or self._message_id_for_state(state, run_id)
        index, _ = _find_message(state, assistant_id)
        if not change.mutations:
            return SimpleNamespace(tool_call_id=tool_call_id), SimpleNamespace(id=part_id)
        part_index = len(state["messages"][index]["parts"]) - 1
        return SimpleNamespace(
            tool_call_id=tool_call_id, part_id=f"{assistant_id}-part-{part_index}"
        ), SimpleNamespace(id=f"{assistant_id}-part-{part_index}")

    def transition_tool_call(
        self, task_id: int, tool_call_id: str, status: str, run_id: int | None = None
    ) -> object | None:
        """校验并迁移工具调用状态。"""

        if status not in {"pending", "running", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported tool status")
        allowed = {
            "pending": {"pending", "running", "failed", "cancelled"},
            "running": {"running", "completed", "failed", "cancelled"},
            "completed": {"completed"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            index, part_index = _find_tool(state, tool_call_id)
            current = state["messages"][index]["parts"][part_index]["status"]
            if status not in allowed[current]:
                raise ValueError(f"invalid tool transition {current} -> {status}")
            return [ConversationStateMutation(
                "set", ("messages", index, "parts", part_index, "status"), status
            )]

        self._snapshots.apply_planned(task_id, plan)
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
        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            index, part_index = _find_tool(state, tool_call_id)
            current = state["messages"][index]["parts"][part_index]["status"]
            if current in {"completed", "failed", "cancelled"}:
                if current != status:
                    raise ValueError(f"terminal tool status conflict: {current} vs {status}")
                return []
            return [
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
            ]
        change = self._snapshots.apply_planned(task_id, plan)
        if not change.mutations:
            return SimpleNamespace(tool_call_id=tool_call_id, status=status)
        return SimpleNamespace(tool_call_id=tool_call_id, status=status)

    def settle_open_tool_calls(self, run_id: int, status: str, reason: str) -> None:
        """将指定 Run 遗留的 pending/running tools 收束为 failed/cancelled。"""

        task_id = self._task_id_for_run(run_id)
        target = "cancelled" if status == "cancelled" else "failed"
        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            mutations: list[ConversationStateMutation] = []
            for index, message in enumerate(state["messages"]):
                if message.get("runId") != run_id:
                    continue
                for part_index, part in enumerate(message["parts"]):
                    if not (isinstance(part, dict) and part.get("type") == "tool-call"):
                        continue
                    if part.get("status") not in {"pending", "running"}:
                        continue
                    base = ("messages", index, "parts", part_index)
                    mutations.extend([
                        ConversationStateMutation("set", base + ("status",), target),
                        ConversationStateMutation("set", base + ("result",), None),
                        ConversationStateMutation("set", base + ("error",), reason),
                        ConversationStateMutation("set", base + ("isError",), target == "failed"),
                    ])
            return mutations

        self._snapshots.apply_planned(task_id, plan)

    def set_run_snapshot(
        self, task_id: int, run_id: int, status: str, end_reason: str | None = None
    ) -> None:
        """只更新指定 Run 在 Transport snapshot 中的展示状态。"""

        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            mutations = [ConversationStateMutation("set", ("run", "status"), status)]
            index = find_assistant_message_index(state, run_id)
            if index is not None:
                mutations.extend([
                    ConversationStateMutation("set", ("messages", index, "status"), status),
                    ConversationStateMutation("set", ("messages", index, "endReason"), end_reason),
                ])
            return mutations

        self._snapshots.apply_planned(task_id, plan)

    def recover_run_snapshot(self, task_id: int, run_id: int) -> None:
        """只收束 backend 重启后 snapshot 中遗留的运行展示状态。"""

        def plan(state: ConversationStateSnapshot) -> list[ConversationStateMutation]:
            mutations: list[ConversationStateMutation] = []
            index = find_assistant_message_index(state, run_id)
            if index is not None:
                for part_index, part in enumerate(state["messages"][index]["parts"]):
                    if isinstance(part, dict) and part.get("type") == "tool-call" \
                            and part.get("status") in {"pending", "running"}:
                        base = ("messages", index, "parts", part_index)
                        mutations.extend([
                            ConversationStateMutation("set", base + ("status",), "failed"),
                            ConversationStateMutation("set", base + ("error",), "execution_interrupted"),
                            ConversationStateMutation("set", base + ("isError",), True),
                        ])
                mutations.extend([
                    ConversationStateMutation("set", ("messages", index, "status"), "failed"),
                    ConversationStateMutation("set", ("messages", index, "endReason"), "backend_restarted"),
                ])
            mutations.append(ConversationStateMutation("set", ("run", "status"), "failed"))
            return mutations

        self._snapshots.apply_planned(task_id, plan)

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

        state = self._snapshots.ensure_state_snapshot(task_id)
        return self._message_id_for_state(state, run_id)

    @staticmethod
    def _message_id_for_state(state: ConversationStateSnapshot, run_id: int | None) -> str:
        """从 snapshot 中查找指定 Run 的 assistant 消息。"""

        for message in state["messages"]:
            if message.get("runId") == run_id and message.get("role") == "assistant":
                return str(message["id"])
        raise KeyError(f"assistant message for run {run_id} not found")


def find_assistant_message_index(
    state: ConversationStateSnapshot, run_id: int
) -> int | None:
    """在 snapshot 中定位指定 Run 的 assistant 消息。"""

    for index, message in enumerate(state["messages"]):
        if message.get("runId") == run_id and message.get("role") == "assistant":
            return index
    return None


def _message(
    message_id: str,
    run_id: int | None,
    role: Literal["user", "assistant"],
    status: str,
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
