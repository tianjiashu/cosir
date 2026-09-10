"""模型流式输出的 part 生命周期状态机。

把模型 ``text`` / ``reasoning`` 两个**交错**通道收敛为前端 wire 协议要求的**顺序括号化**
part 事件流：首个 delta 隐式开启一个 part，切通道 / 出 tool_call / 流结束 / 被取消时发出
``AssistantPartClosedEvent`` 收口，part 之间顺序而非重叠。

本模块只承载「part 生命周期」单一职责，是 ``model_node`` 流式循环的辅助单元；不负责取消 /
usage / 终态语义，那些由调用方处理。状态机持有「当前处于开启态的 part」这一唯一状态，把原先
散落在流式循环多处（切通道先关旧 part、tool_call / 取消 / 流末收口）的开关逻辑收口到一处。
"""

from collections.abc import Callable

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    AssistantTextPartKind,
)


class StreamingPartStateMachine:
    """把交错的 text/reasoning chunk 流收敛为顺序括号化的 part 事件流。

    模型流式输出中 ``text`` 与 ``reasoning`` 两个通道会交错出现；前端 wire 协议要求每个
    通道是一个带边界的 part：从首个 delta 隐式开启，到「切到另一通道 / 出现 tool_call /
    流结束 / 被取消」时发出 ``AssistantPartClosedEvent`` 收口，且 part 之间顺序而非重叠。

    本状态机持有「当前处于开启态的 part」这一唯一状态，把原先散落在流式循环多处（切通道先
    关旧 part、tool_call/取消/流末收口）的开关逻辑收口到一处；调用方只需声明「来了一段
    text / reasoning / 流结束」，由本机决定何时开、何时关、发什么事件，循环本身不再知道
    part 切换细节。

    状态:
        ``_active``: 当前已开启但未收口的 part（``"text"`` / ``"reasoning"`` / ``None``）。

    输入（公开方法）:
        ``text(delta)`` / ``reasoning(delta)``：追加一段增量并维持该通道为 active。
        ``tool_call()`` / ``finish()``：强制收口当前 active part（tool_call 打断或流末/取消）。

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
    ) -> None:
        """构造 part 生命周期状态机。

        参数:
            stream_writer: LangGraph custom stream 写入器，用于发出 part 相关事件。
            task_id: 所属任务 ID，写入事件信封。
            run_id: 所属 run ID，写入事件信封。
            step_id: 当前 step ID，写入事件信封。
        """

        self._stream_writer = stream_writer
        self._task_id: int = task_id
        self._run_id: int = run_id
        self._step_id: str = step_id
        self._active: AssistantTextPartKind | None = None

    def text(self, delta: str) -> None:
        """追加一段正文增量，并确保 ``text`` 通道处于 active 态（必要时先收口旧 part）。

        参数:
            delta: 非空正文增量（调用方保证非空，因 ``AssistantTextDeltaEvent`` 拒绝空串）。
        """

        self._switch_to("text")
        self._stream_writer(
            AssistantTextDeltaEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                part="text",
                delta=delta,
            )
        )

    def reasoning(self, delta: str) -> None:
        """追加一段思考增量，并确保 ``reasoning`` 通道处于 active 态（必要时先收口旧 part）。

        参数:
            delta: 非空思考增量（调用方保证非空）。
        """

        self._switch_to("reasoning")
        self._stream_writer(
            AssistantTextDeltaEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                part="reasoning",
                delta=delta,
            )
        )

    def tool_call(self) -> None:
        """模型产出 tool_call 时收口当前 active part。

        tool_call 创建事件只能在聚合消息后发出，此处先收口文本 / 推理 part，避免 Transport
        把「reasoning 仍在 running」与后续工具创建绑定在一起。
        """

        self._close()

    def finish(self) -> None:
        """流结束或被取消时收口当前 active part（幂等：无 active 时不发事件）。"""

        self._close()

    def _switch_to(self, part: AssistantTextPartKind) -> None:
        """切换到目标通道：若当前 active 不是该通道，先收口旧 part 再置为新通道。

        参数:
            part: 目标通道类型（``"text"`` / ``"reasoning"``）。
        """

        if self._active != part:
            self._close()
            self._active = part

    def _close(self) -> None:
        """收口当前 active part：发出 ``AssistantPartClosedEvent`` 并清空 active 状态。

        幂等：``_active`` 为 ``None`` 时直接返回，不发出任何事件。
        """

        if self._active is None:
            return
        self._stream_writer(
            AssistantPartClosedEvent(
                task_id=self._task_id,
                run_id=self._run_id,
                step_id=self._step_id,
                part=self._active,
            )
        )
        self._active = None
