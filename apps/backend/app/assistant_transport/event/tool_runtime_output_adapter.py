"""把进程工具输出通道适配为运行期事件。"""

from __future__ import annotations

import asyncio

from app.core.events import EventSink, TerminalOutputDeltaData, ToolCallRuntimeUpdateEvent
from app.core.tools.schemas.tool_names import TOOL_EXECUTE_TERMINAL
from app.core.tools.schemas.tool_output import (
    ProcessToolOutputChannel,
    ProcessToolOutputChannelFactory,
)
from app.core.tools.tool_execute.tool_output_channel import (
    BufferedProcessToolOutputChannel,
)

_EVENT_TEXT_LIMIT = 4096


class ToolRuntimeOutputChannelFactory(ProcessToolOutputChannelFactory):
    """为终端进程工具创建输出通道，把输出增量作为运行期事件交给注入的 sink。"""

    def __init__(self, event_sink: EventSink) -> None:
        self._event_sink = event_sink

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
            tool_name != TOOL_EXECUTE_TERMINAL
            or task_id < 1
            or run_id < 1
            or not tool_call_id
        ):
            return None

        def publish(seq: int, text: str) -> None:
            self._event_sink(
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
