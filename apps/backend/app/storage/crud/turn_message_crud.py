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

    def save_messages(self, turn_id: str, messages: list[RuntimeMessage]) -> None:
        """覆盖式保存某 turn 的有序消息轨迹。

        先删除该 turn 的既有轨迹，再按 ``sequence`` 顺序插入新轨迹，保证幂等可重放。

        参数:
            turn_id: 所属轮次标识。
            messages: 有序的运行时消息列表（模型无关）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            删除并重新插入 ``turn_messages`` 表中该 turn 的对应行。
        """

        with self._session_factory.begin() as session:
            session.execute(
                delete(TurnMessageModel).where(TurnMessageModel.turn_id == turn_id)
            )
            for sequence, message in enumerate(messages):
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
