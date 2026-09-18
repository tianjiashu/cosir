"""把进程工具输出通道适配为 Assistant Transport 运行期事件。"""

from __future__ import annotations

import asyncio

from app.assistant_transport.event import (
    TerminalOutputDeltaData,
    ToolCallRuntimeUpdateEvent,
)
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.core.tools.schemas.tool_output import (
    ProcessToolOutputChannel,
    ProcessToolOutputChannelFactory,
)
from app.core.tools.tool_execute.tool_output_channel import (
    BufferedProcessToolOutputChannel,
)

_EVENT_TEXT_LIMIT = 4096


class ToolRuntimeOutputChannelFactory(ProcessToolOutputChannelFactory):
    """为终端进程工具创建带 Assistant Transport 事件发布函数的输出通道。"""

    def create(
        self,
        *,
        task_id: int,
        run_id: int,
        tool_call_id: str,
        tool_name: str,
        loop: asyncio.AbstractEventLoop,
    ) -> ProcessToolOutputChannel | None:
        """仅为有效的终端工具调用创建输出通道。"""
        if (
            tool_name != "execute_terminal"
            or task_id < 1
            or run_id < 1
            or not tool_call_id
        ):
            return None

        def publish(seq: int, text: str) -> None:
            dispatch_conversation_event(
                ToolCallRuntimeUpdateEvent(
                    task_id=task_id,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    seq=seq,
                    data=TerminalOutputDeltaData(
                        kind="terminal_output_delta",
                        text=text,
                    ),
                )
            )

        return BufferedProcessToolOutputChannel(
            task_id=task_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            loop=loop,
            publish=publish,
            max_chunk_chars=_EVENT_TEXT_LIMIT,
        )


__all__ = ["ToolRuntimeOutputChannelFactory"]
