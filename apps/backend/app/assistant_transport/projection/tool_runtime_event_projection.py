"""运行中工具调用的运行期更新到 snapshot mutation 的投影。"""

from __future__ import annotations

from collections.abc import Sequence

from app.assistant_transport.projection.snapshot_navigation import find_tool
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_part import ConversationStateToolCallPart
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.config.logging.logger import log
from app.core.events import (
    TerminalCommandStartedData,
    TerminalOutputDeltaData,
    ToolCallRuntimeUpdateEvent,
)
from app.core.tool.schemas.tool_names import TOOL_EXECUTE_TERMINAL
from app.core.tool.terminal_output_window import trim_output_window


def plan_tool_call_runtime_update(
    event: ToolCallRuntimeUpdateEvent,
    state: ConversationStateSnapshot,
) -> Sequence[ConversationStateMutation]:
    """只把运行期数据投影到兼容的 tool-call part；目标缺失时跳过并留痕。

    是否还接收更新只看 part 自己的状态，不看所属 Run：转到后台的
    命令在 Run 挂起、结束甚至开了新 Run 之后仍在输出，Run 状态与命令是否还活着无关。
    """

    found = find_tool(state, event.tool_call_id)
    if found is None:
        log.warning(
            "tool_runtime_update_part_missing",
            extra={
                "msg": "运行期工具投影目标不存在，跳过事件",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "tool_call_id": event.tool_call_id,
                },
            },
        )
        return []
    base, part = found
    status = part.get("status")
    # 命令还活着：前台调用是 ``running``，转到后台的是带 ``background`` 的 ``pending`` 占位。命令结束、
    # 取消或被撤销时 part 都会离开这两种状态，迟到的更新因此自然丢弃。
    if part.get("toolName") != TOOL_EXECUTE_TERMINAL or not (
        status == "running" or (status == "pending" and "background" in part)
    ):
        return []
    if isinstance(event.data, TerminalCommandStartedData):
        return _plan_terminal_started(event.data, part, base)
    return _plan_terminal_output(event, event.data, part, base)


def _plan_terminal_started(
    data: TerminalCommandStartedData,
    part: ConversationStateToolCallPart,
    base: tuple[str | int, ...],
) -> Sequence[ConversationStateMutation]:
    """建立卡片的初始展示数据；part 已有展示（重复投递）时不覆盖。"""

    if part.get("display_data") is not None:
        return []
    return [ConversationStateMutation("set", (*base, "display_data"), dict(data.display_data))]


def _plan_terminal_output(
    event: ToolCallRuntimeUpdateEvent,
    data: TerminalOutputDeltaData,
    part: ConversationStateToolCallPart,
    base: tuple[str | int, ...],
) -> Sequence[ConversationStateMutation]:
    """往初始展示数据上追加输出块；``seq`` 挡住重复帧。

    初始展示由启动更新建立、必然先到（见 ``LiveOutputChannel.start``）；缺失只可能是它发布失败，
    此时丢弃输出并留痕，最终工具结果仍会带完整展示数据到达。卡片输出只保留最近一段，规则与终态展示
    数据同源（``trim_output_window``）；终态展示数据到达后整体覆盖实时预览。
    """

    previous_seq = part.get("terminal_output_seq")
    if isinstance(previous_seq, int) and previous_seq >= event.seq:
        return []

    display_data = part.get("display_data")
    if display_data is None or display_data.get("kind") != "terminal-result":
        log.warning(
            "terminal_output_display_missing",
            extra={
                "msg": "运行中命令没有初始展示数据，丢弃输出块",
                "data": {
                    "task_id": event.task_id,
                    "run_id": event.run_id,
                    "tool_call_id": event.tool_call_id,
                    "seq": event.seq,
                },
            },
        )
        return []
    current_output = display_data.get("output")
    # 启动展示还没有 ``output``：第一块直接写入；之后追加，超出窗口时整体替换并累计省略行数。
    combined = (current_output if isinstance(current_output, str) else "") + data.text
    kept, dropped = trim_output_window(combined)
    mutations: list[ConversationStateMutation]
    if isinstance(current_output, str) and dropped == 0:
        mutations = [
            ConversationStateMutation("append-text", (*base, "display_data", "output"), data.text)
        ]
    else:
        mutations = [ConversationStateMutation("set", (*base, "display_data", "output"), kept)]
        if dropped > 0:
            previous = display_data.get("output_omitted_lines")
            omitted = (previous if isinstance(previous, int) else 0) + dropped
            mutations.append(
                ConversationStateMutation(
                    "set", (*base, "display_data", "output_omitted_lines"), omitted
                )
            )
    mutations.append(ConversationStateMutation("set", (*base, "terminal_output_seq"), event.seq))
    return mutations
