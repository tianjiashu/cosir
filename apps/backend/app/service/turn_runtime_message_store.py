from app.core.context.runtime_message_store import RuntimeMessageStore
from app.models import RuntimeMessage
from app.service.task.turn_service import TurnService


class TurnRuntimeMessageStore(RuntimeMessageStore):
    """``RuntimeMessageStore`` 协议在 service 层的适配实现。

    基于现有 ``TurnService`` 三方法（``append_turn_message`` / ``clear_turn_messages`` /
    ``load_turn_messages``）组合为 ``core/context.RuntimeMessageStore`` 端口，供
    ``RuntimeContextManager`` 注入，避免 ``core/context`` 反向依赖 service。

    职责边界：
    - 负责：把 ``RuntimeMessageStore`` 协议的方法映射到 ``TurnService`` 的既有能力。
    - 不负责：序号自增（由 manager 维护并传入）；跨 turn 顺序拼装的业务语义
      （由协议调用方 ``RuntimeContextManager.build_for_task`` 决定）。
    """

    def __init__(self, turn_service: TurnService) -> None:
        """初始化适配实现并持有底层 ``TurnService`` 引用。

        参数:
            turn_service: 已装配的 ``TurnService`` 实例，提供消息轨迹三方法。

        返回:
            无。

        异常:
            无。

        副作用:
            保存 ``turn_service`` 引用。
        """
        self._turn_service = turn_service

    def append(self, turn_id: int, message: RuntimeMessage, sequence: int) -> None:
        """落库一条消息（转发到 ``TurnService.append_turn_message``）。

        参数:
            turn_id: 目标 turn 标识。
            message: 单条模型无关的运行时消息。
            sequence: 轮内自增序号（由 manager 维护）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败（透传）。
        """
        self._turn_service.append_turn_message(turn_id, message, sequence)

    def clear(self, turn_id: int) -> None:
        """清空某 turn 的全部消息（转发到 ``TurnService.clear_turn_messages``）。

        参数:
            turn_id: 目标 turn 标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败（透传）。
        """
        self._turn_service.clear_turn_messages(turn_id)

    def build_for_task(
        self,
        task_id: int,
    ) -> list[RuntimeMessage]:
        """按 task 维度读回有序历史（含跨轮、排除指定 turn）。

        组合 ``TurnService.list_turns_for_task`` 与 ``load_turn_messages``，跳过
        ``excluded_turn_ids`` 中的 turn，按任务内 turn 顺序返回消息列表。

        参数:
            task_id: 目标 task 标识。

        返回:
            按 turn 顺序排列的 ``RuntimeMessage`` 列表；无历史时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库读取失败（透传）。
        """
        messages: list[RuntimeMessage] = []
        for turn in self._turn_service.list_turns_for_task(task_id):
            messages.extend(self._turn_service.load_turn_messages(turn.id))
        return messages