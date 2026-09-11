"""模型流式输出的 part 生命周期状态机。

把模型 ``text`` / ``reasoning`` 两个**交错**通道收敛为前端 wire 协议要求的**顺序括号化**
part 事件流：首个 delta 隐式开启一个 part，切通道 / 出 tool_call / 流结束 / 被取消时发出
``AssistantPartClosedEvent`` 收口，part 之间顺序而非重叠。

同一通道的连续增量在发出前先累积，达到阈值（字符数或时间）才合并成一条
``AssistantTextDeltaEvent``：下游每个事件都伴随一次全量快照投影、一次 SSE flush 和一次前端
渲染，合并后这些成本随事件数同比例下降，而 ``append-text`` 投影语义对合并后的长增量完全
兼容。所有收口路径（切通道 / tool_call / 流末 / 取消）一定先发出待发缓冲，保证「某 part 的
内容在它被 closed 之前全部到达」这一顺序不变式。

本模块只承载「part 生命周期」单一职责，是 ``model_node`` 流式循环的辅助单元；不负责取消 /
usage / 终态语义，那些由调用方处理。状态机持有「当前处于开启态的 part」与「该 part 尚未发出
的增量缓冲」两个状态，把原先散落在流式循环多处（切通道先关旧 part、tool_call / 取消 / 流末
收口）的开关逻辑收口到一处。
"""

import time
from collections.abc import Callable

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    AssistantTextPartKind,
)

# 单次事件合并的最小字符数；≤0 时该阈值恒满足，合并退化为每次增量立即发出。
DEFAULT_TEXT_FLUSH_MIN_CHARS = 32
# 距上次发出达到该秒数即强制发出，避免短回复在字符阈值未满时长时间不出字；
# ≤0 时该阈值恒满足，同样退化为每次增量立即发出。
DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS = 0.05


class StreamingPartStateMachine:
    """把交错的 text/reasoning chunk 流收敛为顺序括号化的 part 事件流。

    模型流式输出中 ``text`` 与 ``reasoning`` 两个通道会交错出现；前端 wire 协议要求每个
    通道是一个带边界的 part：从首个 delta 隐式开启，到「切到另一通道 / 出现 tool_call /
    流结束 / 被取消」时发出 ``AssistantPartClosedEvent`` 收口，且 part 之间顺序而非重叠。

    本状态机持有「当前处于开启态的 part」与「该 part 尚未发出的增量缓冲」两个状态，把原先
    散落在流式循环多处（切通道先关旧 part、tool_call/取消/流末收口）的开关逻辑收口到一处；
    调用方只需声明「来了一段 text / reasoning / 流结束」，由本机决定何时开、何时关、何时合并
    发出，循环本身不再知道 part 切换与合并细节。

    状态:
        ``_active``: 当前已开启但未收口的 part（``"text"`` / ``"reasoning"`` / ``None``）。
        ``_pending``: 该 active part 上尚未发出的增量文本（合并缓冲，只归属 active 通道）。

    输入（公开方法）:
        ``text(delta)`` / ``reasoning(delta)``：追加一段增量并维持该通道为 active；是否立即
        发出由合并阈值决定。
        ``tool_call()`` / ``finish()``：先发出待发缓冲，再收口当前 active part
        （tool_call 打断或流末/取消）。

    副作用:
        经注入的 ``stream_writer`` 发出 ``AssistantTextDeltaEvent`` 与
        ``AssistantPartClosedEvent``；不负责取消 / usage / 终态语义，那些由调用方处理。
    """

    def __init__(
        self,
        stream_writer: Callable[..., None],
        *,
        task_id: int,
        run_id: int,
        step_id: str,
        text_flush_min_chars: int = DEFAULT_TEXT_FLUSH_MIN_CHARS,
        text_flush_max_interval_seconds: float = DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        """构造 part 生命周期状态机。

        参数:
            stream_writer: LangGraph custom stream 写入器，用于发出 part 相关事件。
            task_id: 所属任务 ID，写入事件信封。
            run_id: 所属 run ID，写入事件信封。
            step_id: 当前 step ID，写入事件信封。
            text_flush_min_chars: 单次事件合并的最小字符数；≤0 时字符阈值恒满足。
            text_flush_max_interval_seconds: 距上次发出达到该秒数即强制发出（时间兜底）；
                ≤0 时时间阈值恒满足。任一阈值恒满足都会让合并退化为每次增量立即发出。
            now: 单调时钟，用于时间阈值判定；测试可注入可控时钟，默认
                ``time.monotonic``。

        副作用:
            无；只初始化状态与依赖。
        """

        self._stream_writer = stream_writer
        self._task_id: int = task_id
        self._run_id: int = run_id
        self._step_id: str = step_id
        self._min_chars: int = text_flush_min_chars
        self._max_interval: float = text_flush_max_interval_seconds
        self._now: Callable[[], float] = now or time.monotonic
        self._active: AssistantTextPartKind | None = None
        self._pending: str = ""
        # 以构造时刻为时间基线：模型首 token 延迟通常大于时间阈值，因此首个增量会立即
        # 发出，首字延迟不被合并策略放大。
        self._last_flush_at: float = self._now()

    def text(self, delta: str) -> None:
        """追加一段正文增量，并确保 ``text`` 通道处于 active 态（必要时先收口旧 part）。

        增量先并入待发缓冲；合并阈值满足时才作为一条事件发出，未满足时由后续增量或收口路径
        （``tool_call()`` / ``finish()`` / 通道切换）负责发出。

        参数:
            delta: 非空正文增量（调用方保证非空，因 ``AssistantTextDeltaEvent`` 拒绝空串）。

        副作用:
            达到阈值时经 ``stream_writer`` 发出 ``AssistantTextDeltaEvent``；切换通道时先
            发出旧通道的待发缓冲与 ``AssistantPartClosedEvent``。
        """

        self._switch_to("text")
        self._append_to_pending(delta)

    def reasoning(self, delta: str) -> None:
        """追加一段思考增量，并确保 ``reasoning`` 通道处于 active 态（必要时先收口旧 part）。

        与 ``text()`` 完全同构，只作用于 ``reasoning`` 通道。

        参数:
            delta: 非空思考增量（调用方保证非空）。

        副作用:
            同 ``text()``，作用在 ``reasoning`` 通道。
        """

        self._switch_to("reasoning")
        self._append_to_pending(delta)

    def tool_call(self) -> None:
        """模型产出 tool_call 时收口当前 active part。

        tool_call 创建事件只能在聚合消息后发出，此处先收口文本 / 推理 part，避免 Transport
        把「reasoning 仍在 running」与后续工具创建绑定在一起。

        副作用:
            先发出待发缓冲（保证该 part 的文本不丢），再发出 ``AssistantPartClosedEvent``。
        """

        self._close()

    def finish(self) -> None:
        """流结束或被取消时收口当前 active part（幂等：无 active 时不发事件）。

        副作用:
            有 active part 时先发出待发缓冲，再发出 ``AssistantPartClosedEvent``。
        """

        self._close()

    def _append_to_pending(self, delta: str) -> None:
        """把增量并入待发缓冲，满足合并阈值时立即发出。

        参数:
            delta: 非空增量文本。

        副作用:
            阈值满足时经 ``stream_writer`` 发出合并后的 ``AssistantTextDeltaEvent``。
        """

        self._pending += delta
        if self._should_flush():
            self._flush_pending()

    def _should_flush(self) -> bool:
        """返回待发缓冲是否应发出。

        返回:
            缓冲非空且（字符数达阈值 或 距上次发出超过时间阈值）时为 ``True``。
        """

        if not self._pending:
            return False
        if len(self._pending) >= self._min_chars:
            return True
        return (self._now() - self._last_flush_at) >= self._max_interval

    def _flush_pending(self) -> None:
        """发出待发缓冲（幂等：缓冲为空或没有 active part 时不发事件）。

        副作用:
            经 ``stream_writer`` 发出 ``AssistantTextDeltaEvent``，清空缓冲并刷新时间基线。
        """

        if not self._pending or self._active is None:
            return
        self._stream_writer(
            AssistantTextDeltaEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                part=self._active,
                delta=self._pending,
            )
        )
        self._pending = ""
        self._last_flush_at = self._now()

    def _switch_to(self, part: AssistantTextPartKind) -> None:
        """切换到目标通道：若当前 active 不是该通道，先收口旧 part 再置为新通道。

        ``_close()`` 会先发出旧通道的待发缓冲，因此「旧通道 delta → 旧 part closed → 新通道
        delta」的顺序始终成立。

        参数:
            part: 目标通道类型（``"text"`` / ``"reasoning"``）。

        副作用:
            通道变化时发出旧通道的待发缓冲与 ``AssistantPartClosedEvent``。
        """

        if self._active != part:
            self._close()
            self._active = part

    def _close(self) -> None:
        """收口当前 active part：先发出待发缓冲，再发出 ``AssistantPartClosedEvent``。

        幂等：``_active`` 为 ``None`` 时直接返回，不发出任何事件。所有收口路径（通道切换、
        ``tool_call()``、``finish()``）都经过本方法，因此「part 的内容在该 part 被 closed 之前
        全部到达」这一顺序不变式只在此处维护一处。

        副作用:
            经 ``stream_writer`` 发出 ``AssistantTextDeltaEvent``（有待发缓冲时）与
            ``AssistantPartClosedEvent``，随后清空 active 状态。
        """

        if self._active is None:
            return
        self._flush_pending()
        self._stream_writer(
            AssistantPartClosedEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                part=self._active,
            )
        )
        self._active = None
