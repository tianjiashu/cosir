"""Conversation mutation 的低耦合端口。

具体的 Assistant Transport/context writer 仍位于 ``app.assistant_transport``；本模块只
声明 service/core 使用的协作协议，避免下层直接依赖 Transport 实现。
"""

from typing import Any, Protocol

from sqlalchemy.orm import Session


class ConversationMutationPort(Protocol):
    """声明运行时和命令编排所需的 conversation writer 接口。"""

    def create_run_messages_in_session(
        self, session: Session, task_id: int, run_id: int, input_text: str
    ) -> Any:
        """在已有事务中创建 Run 的消息基线。"""

    def append_assistant_part_for_run(
        self, task_id: int, run_id: int, part_type: str, text: str
    ) -> Any:
        """追加 assistant reasoning/text 增量。"""

    def create_tool_call(
        self,
        task_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        run_id: int | None = None,
    ) -> Any:
        """创建工具调用 snapshot fact。"""

    def transition_tool_call(
        self, task_id: int, tool_call_id: str, status: str, run_id: int | None = None
    ) -> Any:
        """迁移工具调用状态。"""

    def complete_tool_call_by_external_id(
        self,
        task_id: int,
        tool_call_id: str,
        content: Any,
        run_id: int | None = None,
    ) -> Any:
        """完成工具调用并写入 ToolMessage。"""

    def persist_ai_message_with_tool_calls(
        self, task_id: int, run_id: int, message: Any, calls: list[Any], call_ids: list[str]
    ) -> None:
        """持久化完整 AIMessage 与工具调用。"""

    def claim_pending_run(self, run_id: int) -> bool:
        """认领 pending Run。"""

    def settle_open_tool_calls(self, run_id: int, status: str, reason: str) -> None:
        """收束 Run 中未完成的工具调用。"""
