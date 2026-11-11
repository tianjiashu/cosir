"""单次模型请求的阶段、正文缓冲与工具身份事件；不发射推理原文。

SDK 累计快照由 model 节点保存到 canonical context，观测从那里读取。这里仅根据当前分片
切换阶段；累计工具身份只用于去重，不能把旧调用误当成当前活动。执行准入归 ToolAccessGate，
呈现策略归 Transport；本机不以是否获准执行来隐藏已注册工具的调用。
"""

import time
from collections.abc import Callable

from openai.types.chat import ParsedFunctionToolCall

from app.config.configuration import get_tool_registry
from app.config.constant import Constant
from app.core.context.tool_call_closure import build_placeholder_tool_message
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ModelActivityEvent,
    ToolCallCreatedEvent,
    ToolCallIntentEvent,
    ToolCallStatusChangedEvent,
)
from app.core.events.model_activity_event import ModelActivityPhase
from app.core.tool.call_intent import display_call_arguments
from app.core.workflow.node_helper.common import _message_context, emit
from app.models.message_metadata import ToolMessageMetadata


class ModelStreamStateMachine:
    """每个模型请求一个实例；记录阶段和已发出身份，finish 在所有退出路径执行。"""

    def __init__(self, task_id: int, run_id: int, step_id: str) -> None:
        self._emit = emit
        self._task_id = task_id
        self._run_id = run_id
        self._step_id = step_id
        self._phase: ModelActivityPhase | None = None
        self._calls: dict[str, str] = {}
        self._arguments: dict[str, str] = {}
        self._text_open = False
        self._pending = ""
        self._source_sequence = 0
        # 这次响应所在的回合（随 ``receive`` 带入，一次请求只写一行响应、只属于一个回合）。
        self._turn = 0
        self._min_chars = Constant.Workflow.DEFAULT_TEXT_FLUSH_MIN_CHARS
        self._max_interval = Constant.Workflow.DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS
        self._now: Callable[[], float] = time.monotonic
        # 首 token 延迟计入刷新间隔，避免首字再等待一次缓冲阈值。
        self._last_flush_at = self._now()

    def start(self) -> None:
        """真正进入模型请求前发布等待；仅构造实例不代表请求已开始。"""
        self._publish_phase("waiting")

    def receive(
        self,
        *,
        source_sequence: int,
        turn: int,
        text: str | None,
        reasoning: bool,
        tool_delta: bool,
        tool_calls: list[ParsedFunctionToolCall],
    ) -> None:
        """消费当前分片的通道信号及累计调用身份，推理只接受存在性、不接受原文。

        同一帧多通道时工具优先、其次推理、最后正文。先写完本帧正文，再因推理/工具通道收口；
        旧累计身份既不切阶段也不反复收口正文。尚未完整的身份等 SDK 后续快照补全。
        """
        self._turn = turn
        if tool_delta:
            self._publish_phase("tool_call")
        elif reasoning:
            self._publish_phase("reasoning")
        elif text:
            self._publish_phase("responding")
        if text:
            self.text(text, source_sequence=source_sequence)
        if reasoning or tool_delta:
            self._close()
        self.tool_call(tool_calls)

    def text(self, delta: str, *, source_sequence: int) -> None:
        """缓冲非空正文；阈值或收口触发刷新，保留原始空白与换行。"""
        self._source_sequence = source_sequence
        self._text_open = True
        self._pending += delta
        if (
            len(self._pending) >= self._min_chars
            or self._now() - self._last_flush_at >= self._max_interval
        ):
            self._flush_pending()

    def tool_call(self, raw_tool_calls: list[ParsedFunctionToolCall]) -> None:
        """登记累计快照中新出现的已注册调用；最终快照也走此入口，但不改变模型阶段。"""
        for call in raw_tool_calls:
            # SDK 草稿用 model_construct 构造，字段可能尚未齐全；这是第三方输入边界。
            call_id = getattr(call, "id", None)
            name = getattr(getattr(call, "function", None), "name", None)
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or not isinstance(name, str)
                or not name.strip()
            ):
                continue
            if get_tool_registry().get_tool_definition(name) is None:
                continue
            self._arguments[call_id] = getattr(call.function, "arguments", None) or ""
            if call_id in self._calls:
                continue
            self._close()
            self._calls[call_id] = name
            self._emit(
                ToolCallCreatedEvent(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    step_id=self._step_id,
                    tool_call_id=call_id,
                    tool_name=name,
                    turn=self._turn,
                )
            )

    def finish(self) -> None:
        """无论正常、异常还是取消都收口正文并清除阶段；重复调用不发事件。"""
        try:
            self._close()
            arguments, self._arguments = self._arguments, {}
            for call_id, raw in arguments.items():
                intent, args = display_call_arguments(raw)
                self._emit(ToolCallIntentEvent(
                    task_id=self._task_id, run_id=self._run_id, step_id=self._step_id,
                    tool_call_id=call_id, intent=intent, args=args,
                ))
        finally:
            self._publish_phase(None)

    def cancel(self) -> None:
        """收敛本请求尚未执行的调用，落占位供冷重建；不可在正常完成路径调用。

        每个调用只收敛一次，重复调用不发事件：model 节点的外层失败边界可能在已收敛之后再调用
        （如取消落定写库失败），重复收敛会写出重复占位。"""
        self.finish()
        context = _message_context()
        calls, self._calls = self._calls, {}
        for call_id, name in calls.items():
            self._emit(
                ToolCallStatusChangedEvent(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    step_id=self._step_id,
                    tool_call_id=call_id,
                    status="cancelled",
                )
            )
            context.add_message(
                build_placeholder_tool_message(call_id, name),
                metadata=ToolMessageMetadata(status="cancelled"),
                include_in_context=False,
            )

    def _publish_phase(self, phase: ModelActivityPhase | None) -> None:
        if phase == self._phase:
            return
        self._phase = phase
        self._emit(
            ModelActivityEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                phase=phase,
            )
        )

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        self._emit(
            AssistantTextDeltaEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                source_sequence=self._source_sequence,
                turn=self._turn,
                part="text",
                delta=self._pending,
            )
        )
        self._pending = ""
        self._last_flush_at = self._now()

    def _close(self) -> None:
        if not self._text_open:
            return
        self._flush_pending()
        self._emit(
            AssistantPartClosedEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                source_sequence=self._source_sequence,
                turn=self._turn,
                part="text",
            )
        )
        self._text_open = False
