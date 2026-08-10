"""``turn_messages`` 表的纯 CRUD 数据访问层（每轮消息轨迹）。

单一职责：只提供 ``turn_messages`` 单表的读写与 model↔``RuntimeMessage`` 转换。
每个 turn 的消息按 ``sequence`` 有序存储，承载跨轮记忆与历史回放所需的轨迹。

职责边界：
- 负责：turn 消息轨迹单表读写、``TurnMessageModel``↔``RuntimeMessage`` 转换。
- 不负责：运行时编排、跨表级联、LangChain 消息转换（由 ``core/llm/langchain_bridge``
  的 ``runtime_to_langchain`` 在构建下一轮上下文时单向完成）。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json

from sqlalchemy import delete, select

from app.models.runtime_message import RuntimeMessage
from app.storage.model.turn_message_model import TurnMessageModel
from app.storage.store_engines import main_session_factory


class TurnMessageCrud:
    """``turn_messages`` 表的纯 CRUD。

    仅负责每轮消息轨迹的单表读写与 model↔``RuntimeMessage`` 转换，不承担运行时编排；
    所有方法通过共享主库 session 工厂访问数据库。
    """

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """

        self._session_factory = main_session_factory()

    def append_message(self, turn_id: str, message: RuntimeMessage, sequence: int) -> None:
        """以单条增量方式持久化某 turn 的一条消息（用于逐条落库，替代批覆盖）。

        仅插入一条 ``(turn_id, sequence)`` 记录，不触碰该 turn 的其它行；调用方负责
        在 turn 开始时先 ``clear_turn_messages`` 清掉上一轮残留（崩溃重跑幂等），并维护
        ``sequence`` 在 turn 内的自增连续性。

        参数:
            turn_id: 所属轮次标识。
            message: 单条模型无关的运行时消息。
            sequence: 该消息在本 turn 内的有序序号（从 0 起的连续整数）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``turn_messages`` 表插入一行。
        """

        with self._session_factory.begin() as session:
            session.add(
                TurnMessageModel(
                    turn_id=turn_id,
                    sequence=sequence,
                    role=message.role,
                    content_text=message.content_text,
                    metadata_json=json.dumps(message.metadata, ensure_ascii=False)
                    if message.metadata
                    else None,
                )
            )

    def clear_turn_messages(self, turn_id: str) -> None:
        """删除某 turn 的全部消息轨迹（逐条落库前的幂等清理）。

        与 ``append_message`` 配合：turn 开始执行时先调用本方法清掉上一轮残留，之后
        每条消息经 ``append_message`` 增量写入；历史 turn 的数据因按 ``turn_id`` 隔离
        而不受影响，跨轮拼装天然成立。

        参数:
            turn_id: 待清理消息的轮次标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            从 ``turn_messages`` 表删除该 turn 的全部行。
        """

        with self._session_factory.begin() as session:
            session.execute(delete(TurnMessageModel).where(TurnMessageModel.turn_id == turn_id))

    def load_messages(self, turn_id: str) -> list[RuntimeMessage]:
        """按序读取某 turn 的消息轨迹。

        参数:
            turn_id: 所属轮次标识。

        返回:
            按 ``sequence`` 升序的 ``RuntimeMessage`` 列表；无轨迹时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TurnMessageModel)
                    .where(TurnMessageModel.turn_id == turn_id)
                    .order_by(TurnMessageModel.sequence)
                )
                .scalars()
                .all()
            )
        return [
            RuntimeMessage(
                role=row.role,
                content_text=row.content_text,
                metadata=json.loads(row.metadata_json) if row.metadata_json else {},
            )
            for row in rows
        ]

    def delete_by_turn_ids(self, turn_ids: list[str]) -> None:
        """按轮次标识批量删除消息轨迹（用于任务 / 工作区级联删除）。

        参数:
            turn_ids: 待清理消息的轮次标识列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            turn_ids 非空时从 ``turn_messages`` 表删除匹配的行。
        """

        if not turn_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TurnMessageModel).where(TurnMessageModel.turn_id.in_(turn_ids)))
