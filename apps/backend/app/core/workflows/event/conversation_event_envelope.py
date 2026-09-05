"""Conversation event 的共有信封字段。

本模块只承载「所有 conversation event 都必须携带的定位与排查字段」这一单一职责，
不含任何具体事件类型；具体事件在各域模块中继承本信封并追加自己的 payload 字段。

信封存在的意义：event 描述的是一条**已经发生的事实**（而不是待执行的意图），消费者
必须知道它作用于哪个 task / run、由哪一步产生、何时产生，才能在状态投影、结构化日志
和事后排查中唯一定位。其中 ``step_id`` 与 ``occurred_at`` 只服务于日志与排查，
**不参与状态投影**——快照的最终形状只由 payload 决定。

不负责：事件的分发、排序、持久化、合法性仲裁与状态投影（分别由事件通道与 applier 承担）。
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ConversationEventEnvelope(BaseModel):
    """所有 conversation event 的共有信封。

    提供「作用于谁、由哪一步产生、何时产生」的唯一定位信息。本类不承载任何业务
    payload，也不描述如何把事实投影成 Transport snapshot。

    契约说明：

    - ``extra="forbid"``：event 是进程内契约，字段拼错必须立刻暴露而不是被静默丢弃，
      否则生产者会以为自己发出了事实、消费者却永远看不到。
    - ``frozen=True``：事实一旦产生即不可变，避免消费者改写后再被后续消费者读到脏值。
    - 未知 **类型** 的事件由 applier 侧策略处理（记 warning 后跳过），与本信封无关；
      本信封的 ``extra="forbid"`` 只约束已知类型上的未知字段。

    Attributes:
        task_id: 事实所属 Task 标识，同时是 Transport snapshot 的聚合维度。
        run_id: 事实所属 Conversation Run 标识。
        step_id: 产生该事实的 graph 步标识（如 ``"step-3"``）；纯排查用，不进 snapshot。
            在 workflow 外产生的事实（run 终态、启动恢复、用户输入）置 ``None``。
        occurred_at: 事实产生时刻（UTC，带时区）；纯排查用，不进 snapshot，
            缺省由构造时刻自动生成。

    异常:
        pydantic.ValidationError: ``task_id`` / ``run_id`` 非正整数，或出现未声明字段时抛出。

    副作用:
        无；仅做字段校验，不触碰 storage、网络或运行时状态。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: int = Field(ge=1)
    #ContextUsageUpdatedEvent 无 run_id
    run_id: int | None = None
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    step_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
