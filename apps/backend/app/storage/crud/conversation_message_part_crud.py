"""``conversation_message_parts`` 表的事务内 CRUD。"""

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from app.models.conversation_message_part_record import ConversationMessagePartRecord
from app.storage.model.conversation_message_part_model import ConversationMessagePartModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationMessagePartCrud:
    """提供消息 part 的创建、读取和文本增量更新。"""

    def __init__(self) -> None:
        """绑定主库 session 工厂。"""

        self._session_factory = main_session_factory()

    @staticmethod
    def create_in_session(
        session: Session,
        message_id: int,
        sequence: int,
        part_type: str,
        text: str | None = None,
        data_json: str | None = None,
        status: str = "complete",
    ) -> ConversationMessagePartRecord:
        """在调用方事务中创建一条消息 part 事实。

        参数:
            session: 调用方持有的写事务 session。
            message_id: 所属消息主键。
            sequence: 消息内 part 顺序。
            part_type: 中性 part 类型。
            text: 可选文本内容。
            data_json: 可选结构化 JSON 文本。
            status: part 状态，默认 ``complete``。

        返回:
            已 flush 的 part 记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            插入 ``conversation_message_parts`` 行；事务提交由调用方负责。
        """

        now = to_text(utc_now())
        row = ConversationMessagePartModel(
            message_id=message_id,
            sequence=sequence,
            part_type=part_type,
            text=text,
            data_json=data_json,
            status=status,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return ConversationMessagePartRecord.from_model(row)

    @staticmethod
    def append_text_in_session(
        session: Session,
        part_id: int,
        text: str,
        expected_status: str | None = None,
    ) -> ConversationMessagePartRecord:
        """在调用方事务中向既有文本 part 追加内容。

        参数:
            session: 调用方持有的写事务 session。
            part_id: 文本 part 主键。
            text: 要追加的非空文本片段。
            expected_status: 可选的状态条件；不匹配时不更新。

        返回:
            更新后的 part 记录。

        异常:
            ValueError: ``text`` 为空，或目标不是文本 part。
            KeyError: part 不存在或状态条件不满足。
            sqlalchemy.exc.SQLAlchemyError: 更新失败。

        副作用:
            更新 part 的 ``text`` 与 ``updated_at``；事务提交由调用方负责。
        """

        if not text:
            raise ValueError("text must be non-empty")
        row = session.get(ConversationMessagePartModel, part_id)
        if row is None:
            raise KeyError(part_id)
        if row.part_type != "text":
            raise ValueError("only text parts support append")
        if expected_status is not None and row.status != expected_status:
            raise KeyError(part_id)
        row.text = f"{row.text or ''}{text}"
        row.updated_at = to_text(utc_now())
        session.flush()
        return ConversationMessagePartRecord.from_model(row)

    def list_by_message(self, message_id: int) -> list[ConversationMessagePartRecord]:
        """按消息读取全部 part，并按 sequence 升序返回。

        参数:
            message_id: 消息主键。

        返回:
            有序 part 记录列表；没有 part 时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            打开一次只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ConversationMessagePartModel)
                    .where(ConversationMessagePartModel.message_id == message_id)
                    .order_by(asc(ConversationMessagePartModel.sequence))
                )
                .scalars()
                .all()
            )
        return [ConversationMessagePartRecord.from_model(row) for row in rows]

    def delete_by_message_ids(self, session: Session, message_ids: set[int]) -> None:
        """在调用方事务中删除指定消息的全部 part。

        参数:
            session: 调用方持有的写事务 session。
            message_ids: 待删除的消息主键集合。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败。

        副作用:
            删除对应的 ``conversation_message_parts`` 行。
        """

        if message_ids:
            session.query(ConversationMessagePartModel).filter(
                ConversationMessagePartModel.message_id.in_(message_ids)
            ).delete(synchronize_session=False)
