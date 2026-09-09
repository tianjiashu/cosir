"""Conversation event 的共有信封字段与投影契约。

本模块只承载「所有 conversation event 都必须携带的定位与排查字段」以及「把事实投影为
snapshot mutation 的抽象契约」这一单一职责，不含任何具体事件类型；具体事件在各域模块中
继承本信封并追加自己的 payload 字段与 ``plan`` 实现。

信封存在的意义：event 描述的是一条**已经发生的事实**（而不是待执行的意图），消费者必须知道
它作用于哪个 task / run、由哪一步产生、何时产生，才能在状态投影、结构化日志和事后排查中
唯一定位。其中 ``step_id`` 与 ``occurred_at`` 只服务于日志与排查，**不参与状态投影**。

投影契约：``plan`` 是基类声明的抽象方法，每个具体事件必须实现它——把自身事实翻译成一组
``ConversationStateMutation``。事件因此「自带投影逻辑」，projector 只需调用
``event.plan(state)`` 即可，无需按类型分派。``plan`` 只允许抛出 ``ValueError`` / ``KeyError``
等语义异常（非法状态迁移、引用的消息 / part / 工具不存在），由 projector 向上传播。

不负责：事件的分发、排序、持久化、合法性仲裁（分别由事件通道与 projector 承担）。
"""

from abc import abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot


class ConversationEventEnvelope(BaseModel):
    """所有 conversation event 的共有信封与投影契约。

    提供「作用于谁、由哪一步产生、何时产生」的唯一定位信息，并声明 ``plan`` 抽象方法要求
    子类把事实投影为 snapshot mutation。本类是抽象类（``plan`` 为抽象方法），不可直接实例化，
    必须由具体事件子类实现 ``plan``。本类不承载任何业务 payload，也不描述如何把事实投影成
    Transport snapshot——具体投影由各子类在 ``plan`` 中实现。

    契约说明：

    - ``extra="forbid"``：event 是进程内契约，字段拼错必须立刻暴露而不是被静默丢弃。
    - ``frozen=True``：事实一旦产生即不可变，避免消费者改写后再被后续消费者读到脏值。

    Attributes:
        task_id: 事实所属 Task 标识，同时是 Transport snapshot 的聚合维度。
        run_id: 事实所属 Conversation Run 标识。
        event_id: 事件唯一标识，projector 用于去重。
        step_id: 产生该事实的 graph 步标识；纯排查用，不进 snapshot。
        occurred_at: 事实产生时刻（UTC，带时区）；纯排查用，不进 snapshot。

    异常:
        pydantic.ValidationError: ``task_id`` / ``run_id`` 非正整数，或出现未声明字段时抛出。
        NotImplementedError: 直接实例化抽象基类时抛出（``plan`` 未实现）。

    副作用:
        无；仅做字段校验，不触碰 storage、网络或运行时状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: int = Field(ge=1)
    # ContextUsageUpdatedEvent 无 run_id
    run_id: int | None = None
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    step_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @abstractmethod
    def plan(
        self,
        state: ConversationStateSnapshot,
    ) -> Sequence[ConversationStateMutation]:
        """把本事实投影为一组 snapshot mutation。

        参数:
            state: 当前 Task 的 Transport snapshot（只读，供定位消息 / part / 工具）。

        返回:
            需要施加到 snapshot 的 mutation 序列；无变化时返回空序列。

        异常:
            ValueError: 投影规则不满足（如非法工具状态迁移）。
            KeyError: 事件引用的消息 / part / 工具在 snapshot 中不存在。

        副作用:
            无；只产出 mutation 描述，不直接修改 ``state``。
        """
        raise NotImplementedError
