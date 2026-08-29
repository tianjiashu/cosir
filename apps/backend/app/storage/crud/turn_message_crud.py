"""``turn_messages`` 表的纯 CRUD 数据访问层（每轮消息轨迹）。

单一职责：只提供 ``turn_messages`` 单表的读写与 model↔``RuntimeMessage`` 转换。
每个 turn 的消息按 ``sequence`` 有序存储，承载跨轮记忆与历史回放所需的轨迹。

职责边界：
- 负责：turn 消息轨迹单表读写、``TurnMessageModel``↔``RuntimeMessage`` 转换。
- 不负责：运行时编排、跨表级联、LangChain 消息清洗（由 ``core/llm/langchain_bridge``
  在构建下一轮上下文时完成）。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json

from sqlalchemy import delete, func, select
from sqlalchemy.sql.expression import and_

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

    def append_message(
        self, turn_id: int, message: RuntimeMessage, sequence: int, in_context: bool = True
    ) -> None:
        """以单条增量方式持久化某 turn 的一条消息（用于逐条落库，替代批覆盖）。

        仅插入一条 ``(turn_id, sequence)`` 记录，不触碰该 turn 的其它行；调用方负责
        在 turn 开始时先 ``clear_turn_messages`` 清掉上一轮残留（崩溃重跑幂等），并维护
        ``sequence`` 在 turn 内的自增连续性。

        参数:
            turn_id: 所属轮次标识。
            message: 单条模型无关的运行时消息。
            sequence: 该消息在本 turn 内的有序序号（从 0 起的连续整数）。
            in_context: 是否纳入后续 Agent 上下文；False 表示仅落库轨迹但不参与
                下一轮上下文拼装（``load_messages`` 会过滤掉 in_context 为假的行），
                默认 True。

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
                    in_context=in_context,
                )
            )

    def clear_turn_messages(self, turn_id: int) -> None:
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

    def load_messages(self, turn_id: int) -> list[RuntimeMessage]:
        """按序读取某 turn 的「进模型上下文」消息轨迹。

        仅返回 ``in_context`` 为真的消息（与运行时拼装模型上下文的口径一致），供
        内存恢复 / 下一轮上下文拼装使用，不承载审计或回放职责。

        参数:
            turn_id: 所属轮次标识。

        返回:
            按 ``sequence`` 升序、仅含 ``in_context`` 为真消息的 ``RuntimeMessage`` 列表；
            无轨迹时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TurnMessageModel)
                    .where(and_(TurnMessageModel.in_context, TurnMessageModel.turn_id == turn_id))
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

    def load_messages_full(self, turn_id: int) -> list[RuntimeMessage]:
        """按序读取某 turn 的**完整**消息历史（含 ``in_context`` 为假的消息）。

        与 :meth:`load_messages` 不同，本方法不做 ``in_context`` 过滤，返回该 turn 在
        ``turn_messages`` 表中保存的全部轨迹，供审计、历史回放、change set 等需要完整
        消息历史的场景使用；不应作为拼装模型上下文的数据源。

        参数:
            turn_id: 所属轮次标识。

        返回:
            按 ``sequence`` 升序的 ``RuntimeMessage`` 列表（含全部 ``in_context`` 取值）；
            无轨迹时为空列表。

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

    def next_sequence(self, turn_id: int) -> int:
        """返回指定 turn 下一条消息可用的 sequence，包含隐藏轨迹。"""
        with self._session_factory() as session:
            value = session.execute(
                select(func.max(TurnMessageModel.sequence)).where(
                    TurnMessageModel.turn_id == turn_id
                )
            ).scalar_one()
        return 0 if value is None else int(value) + 1

    def delete_by_ids(self, ids: list[int]) -> None:
        """按轮次标识批量删除消息轨迹（用于任务 / 工作区级联删除）。

        参数:
            ids: 待清理消息的轮次标识列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            ids 非空时从 ``turn_messages`` 表删除匹配的行。
        """

        if not ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TurnMessageModel).where(TurnMessageModel.turn_id.in_(ids)))
