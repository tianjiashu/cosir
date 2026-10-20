"""模型流式输出的 part 生命周期状态机。

把模型 ``text`` / ``reasoning`` 两个**交错**通道收敛为前端 wire 协议要求的**顺序括号化**
part 事件流：首个 delta 隐式开启一个 part，切通道 / 出 tool_call / 流结束 / 被取消时发出
``AssistantPartClosedEvent`` 收口，part 之间顺序而非重叠。

同一通道的连续增量在发出前先累积，达到阈值（字符数或时间）才合并成一条
``AssistantTextDeltaEvent``：下游每个事件都伴随一次全量快照投影、一次 SSE flush 和一次前端
渲染，合并后这些成本随事件数同比例下降，而 ``append-text`` 投影语义对合并后的长增量完全
兼容。所有收口路径（切通道 / tool_call / 流末 / 取消）一定先发出待发缓冲，保证「某 part 的
内容在它被 closed 之前全部到达」这一顺序不变式。

本模块承载「单次模型请求内 part 的生命周期」单一职责，是 ``model_node`` 流式循环的辅助单元；
不负责 usage 与 Run 终态，那些由调用方处理。状态机持有「当前处于开启态的 part」「该 part 尚未
发出的增量缓冲」「本请求已投影给前端的工具调用」三份状态，把原先散落在流式循环多处（切通道先
关旧 part、tool_call / 取消 / 流末收口）的开关逻辑收口到一处。

取消时工具 part 的收敛也归这里（见 :meth:`StreamingPartStateMachine.cancel`）：它的输入正是
本实例记录的「已投影调用」，而 Run 级补偿收束（``ToolCallsSettledEvent``）发生在驱动收尾，
实时性不足以覆盖「流中断那一刻」。
"""

import time
from collections.abc import Callable

from openai.types.chat import ParsedFunctionToolCall

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    AssistantTextPartKind,
    ToolCallCreatedEvent,
    ToolCallStatusChangedEvent,
)
from app.config.constant import Constant
from app.core.context.tool_call_closure import build_placeholder_tool_message
from app.core.workflow.node_helper import get_stream_writer
from app.core.workflow.node_helper.common import _message_context, _runtime_context
from app.core.workflow.workflow_operations import WorkflowOperations
from app.models.message_metadata import ToolMessageMetadata

# 单次事件合并的最小字符数；≤0 时该阈值恒满足，合并退化为每次增量立即发出。
# 距上次发出达到该秒数即强制发出，避免短回复在字符阈值未满时长时间不出字；
# ≤0 时该阈值恒满足，同样退化为每次增量立即发出。


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
        「已投影调用」记账不归本实例，取自 ``RuntimeContext.step_context``：``model_node`` 每次
        请求开始前整体换新，所以它既承担本请求的累积快照去重，也正好是 ``cancel()`` 的收口范围。

    输入（公开方法）:
        ``text(delta)`` / ``reasoning(delta)``：追加一段增量并维持该通道为 active；是否立即
        发出由合并阈值决定。
        ``tool_call(raw_tool_calls)`` / ``finish()``：先发出待发缓冲，再收口当前 active part
        （tool_call 打断或流末）。
        ``cancel()``：收口当前 part，并把已投影的调用收敛为 ``cancelled``。

    副作用:
        经 ``stream_writer`` 发出 ``AssistantTextDeltaEvent``、``AssistantPartClosedEvent``、
        ``ToolCallCreatedEvent``、``ToolCallStatusChangedEvent``；``cancel()`` 还会写仅供展示的
        cancelled tool 占位行。不负责 usage 与 Run 终态语义，那些由调用方处理。
    """

    def __init__(
        self,
        task_id: int,
        run_id: int,
        step_id: str,
    ) -> None:
        """构造 part 生命周期状态机。

        参数:
            task_id: 所属任务 ID，写入事件信封。
            run_id: 所属 run ID，写入事件信封。
            step_id: 当前 step ID，写入事件信封。

        副作用:
            无；只初始化状态与依赖。「已投影调用」记账取自 ``RuntimeContext``，由驱动共享，
            本实例只在 :meth:`tool_call` 写入、在 :meth:`cancel` 读回。
        """

        self._stream_writer = get_stream_writer()
        self._task_id: int = task_id
        self._run_id: int = run_id
        self._step_id: str = step_id
        self._min_chars: int = Constant.Workflow.DEFAULT_TEXT_FLUSH_MIN_CHARS
        self._max_interval: float = Constant.Workflow.DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS
        self._now: Callable[[], float] = time.monotonic
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

    def tool_call(self, raw_tool_calls: list[ParsedFunctionToolCall]) -> None:
        """模型产出 tool_call 时收口当前 active part，并投影新出现的调用身份。

        ``raw_tool_calls`` 取自 SDK 累积快照而非本帧增量：流式期间每帧都带着此前出现过的全部
        调用，故必须按 ``call_id`` 去重，否则同一调用会在每个 chunk 重复建前端 part。工具 part
        由 ``ToolCallCreatedEvent`` 承载，不参与 text/reasoning 的开关状态，因此这里只做
        ``_close()``：先发待发缓冲再收口，保证「旧 part 内容全部到达 → 旧 part closed → 工具
        part」的顺序。

        参数:
            raw_tool_calls: 快照中的 function 调用列表；元素是 SDK pydantic 模型
                （``ParsedFunctionToolCall``），不是 dict。

        副作用:
            先发出待发缓冲与 ``AssistantPartClosedEvent``；随后对每个尚未投影的调用发出一次
            ``ToolCallCreatedEvent``，并记入 ``RuntimeContext.step_context`` 的投影记账。
        """

        self._close()
        if not raw_tool_calls:
            # 空批次只负责收口 part，不必触碰运行期依赖（也避免在无 graph 上下文时取值）。
            return
        projected = _runtime_context().step_context.projected_tool_calls
        for raw_tool_call in raw_tool_calls:
            # 快照是累积草稿：id / name 由 provider 按 index 首帧给出，个别 provider 可能晚一帧
            # 才带上。SDK 用 model_construct 构造快照、不做校验，缺字段会直接 AttributeError 并炸
            # 整个 Run，故在输入边界按「未就绪」处理，等后续帧补全。
            call_id = getattr(raw_tool_call, "id", None)
            tool_name = getattr(getattr(raw_tool_call, "function", None), "name", None)
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or not isinstance(tool_name, str)
                or not tool_name.strip()
                or call_id in projected
            ):
                continue
            if self.create_tool_call(call_id, tool_name):
                projected[call_id] = tool_name

    def finish(self) -> None:
        """流结束或被取消时收口当前 active part（幂等：无 active 时不发事件）。

        副作用:
            有 active part 时先发出待发缓冲，再发出 ``AssistantPartClosedEvent``。
        """

        self._close()

    def cancel(self) -> None:
        """Run 被取消时收口当前 part，并把本请求已投影的调用收敛为 ``cancelled``。

        这里**不判断调用状态**：状态机实例等于一次模型请求，它投影的调用在本请求内必然尚未
        执行（工具要等模型完整产出后才跑），因此不存在把已 ``completed`` 的调用改写成
        ``cancelled`` 的情形。推论：只能在 ``RunStopped`` 分支调用，**不得放进 ``finally``**
        ——那会在正常完成后也执行，用占位行原地覆盖已执行调用的真实结果行。

        重复调用对下游幂等：part 缺失时 projector 记 warning 跳过，``cancelled -> cancelled``
        在迁移白名单内，占位行按 ``tool_call_id`` 覆盖同一行。

        副作用:
            先收口 active part；随后逐条发出 ``ToolCallStatusChangedEvent(cancelled)`` 并写一条
            ``include_in_context=False`` 的 cancelled tool 占位行，使实时 snapshot 与冷重建同源。
        """

        self._close()
        runtime_context = _message_context()
        # 记账由 model_node 在每次请求开始前清空，因此这里只含本请求已投影的调用。
        for call_id, tool_name in _runtime_context().step_context.projected_tool_calls.items():
            self._stream_writer(
                ToolCallStatusChangedEvent(
                    task_id=self._task_id,
                    run_id=self._run_id,
                    step_id=self._step_id,
                    tool_call_id=call_id,
                    status="cancelled",
                )
            )
            runtime_context.add_message(
                build_placeholder_tool_message(call_id, tool_name),
                metadata=ToolMessageMetadata(
                    status="cancelled",
                    display_data=None,
                    error=None,
                ),
                run_id=self._run_id,
                include_in_context=False,
            )

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

    def create_tool_call(self, call_id: str | None, tool_name: str | None) -> bool:
        """投影一次工具调用身份，返回是否真的发出了 ``ToolCallCreatedEvent``。

        返回 ``False`` 的路径都不入已投影集合，调用方据此决定是否记账：id / 工具名缺失或为空属于
        草稿未就绪或解析噪声（后续快照帧会补全），提前记账会让该调用永远没有前端 part；工具名
        未注册同理。参数合法性由工具执行边界裁决，这里只回答「前端能
        不能画出这个气泡」。
        """

        if not call_id or not call_id.strip() or not tool_name or not tool_name.strip():
            return False
        operations: WorkflowOperations = _runtime_context().operations
        if not operations.vaild_tool_name(tool_name):
            return False
        if tool_name not in operations.allows_tools:
            # 禁用工具仍交给执行层形成错误观察，但不创建前端卡片。
            return False
        # 只为本轮放行的工具创建卡片；被拦调用仍保留在模型协议中，由执行层返回错误观察。
        presentation: dict[str, object] = {}
        for definition in operations.all_vaild_tools:
            if definition.name == tool_name and definition.display is not None:
                presentation = definition.display.to_dict()
                break
        self._stream_writer(
            ToolCallCreatedEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                tool_call_id=call_id,
                tool_name=tool_name,
                presentation=presentation,
            )
        )
        return True

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
