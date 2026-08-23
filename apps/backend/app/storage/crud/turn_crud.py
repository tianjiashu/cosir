"""``turns`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``turns`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：turn 单表读写、``TurnModel``↔``TurnRecord`` 转换。
- 不负责：跨表操作与任务编排（由 ``service/task/`` 负责）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from uuid import uuid4

from sqlalchemy import asc, delete, select, update

from app.models import TurnRecord
from app.storage.model.turn_model import TurnModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class TurnCrud:
    """``turns`` 表的纯 CRUD。

    仅负责单表读写与 model↔record 转换，不承担跨表编排；所有方法通过共享主库
    session 工厂访问数据库。
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

    def create(
        self,
        task_id: str,
        input_text: str,
        status: str = "pending",
        agent_id: str | None = None,
        product_id: str | None = None,
        model_id: str | None = None,
        paths: list[str] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
    ) -> TurnRecord:
        """新建一条 turn 记录并落库。

        ``turn_id`` 由本方法生成（UUID4），创建 / 更新时间以当前 UTC 时间统一填充。

        参数:
            task_id: 所属任务标识。
            input_text: 本轮输入文本；不能为空白。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本次轮次绑定的 agent 标识；仅写入应用层 ``TurnRecord``
                值对象（供上层 / 运行时消费），当前 ``turns`` 表模型不持久化
                ``agent_id`` 列，故不落库。
            model_id: 可选，本轮使用的模型条目标识（``ModelEntryRecord.model_id``，
                UUID 字符串）；None 表示创建期未携带（运行期兜底解析后由
                ``update_model_id`` 回写）。
            paths: 可选，本轮输入的路径列表；None 表示无路径关联。
            thinking: 可选，本 turn 是否为思考轮次；None 表示无思考轮次。
            reasoning_effort: 可选，本 turn 思考努力等级；None 表示未指定（由模型侧回退到
                ``max``）。该值经 ``turn_service.create_turn`` 透传落库到
                ``turns.reasoning_effort``。

        返回:
            落库成功的 ``TurnRecord``。

        异常:
            ValueError: 如果 input_text 去除首尾空白后为空。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``turns`` 表插入一行（不含 ``agent_id`` 列，含 ``model_id`` 列）。
        """

        if not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        now = utc_now()
        turn = TurnRecord(
            str(uuid4()),
            task_id,
            input_text,
            status,
            now,
            now,
            product_id=product_id,
            response_text=None,
            agent_id=agent_id,
            model_id=model_id,
            paths=paths,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
        with self._session_factory.begin() as session:
            session.add(
                TurnModel(
                    turn_id=turn.turn_id,
                    task_id=turn.task_id,
                    input_text=turn.input_text,
                    status=turn.status,
                    end_reason=turn.end_reason,
                    response_text=turn.response_text,
                    product_name=turn.product_id,
                    model_name=turn.model_id,
                    created_at=to_text(turn.created_at),
                    updated_at=to_text(turn.updated_at),
                    paths=turn.paths,
                    thinking=turn.thinking,
                    reasoning_effort=turn.reasoning_effort,
                )
            )
        return turn

    def get(self, turn_id: str) -> TurnRecord:
        """按标识返回单个 turn。

        参数:
            turn_id: turn 标识。

        返回:
            匹配的 ``TurnRecord``。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row: TurnModel | None = session.get(TurnModel, turn_id)
        if row is None:
            raise KeyError(turn_id)
        return TurnRecord.from_model(row)

    def list_by_task(self, task_id: str) -> list[TurnRecord]:
        """列出某任务下的全部 turn，按创建时间升序。

        参数:
            task_id: 任务标识。

        返回:
            该任务的 turn 列表，按 ``created_at`` 再 ``turn_id`` 升序；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TurnModel)
                    .where(TurnModel.task_id == task_id)
                    .order_by(asc(TurnModel.created_at), asc(TurnModel.turn_id))
                )
                .scalars()
                .all()
            )
        return [TurnRecord.from_model(row) for row in rows]

    def update_status_if_in(
        self,
        turn_id: str,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        end_reason: str | None = None,
        response_text: str | None = None,
    ) -> TurnRecord | None:
        """以乐观锁方式把 turn 更新为目标状态，仅当其当前状态在允许集合内。

        纯数据访问操作：不携带任何业务语义（如「cancel」「fail」的意图），只负责
        ``WHERE status IN (allowed_statuses)`` 条件下的原子更新，用于避免并发收尾请求
        改写历史终态。调用方（service 层）负责决定目标状态、允许的前置状态集合与
        伴随字段，CRUD 层不判断业务合法性。

        参数:
            turn_id: turn 标识。
            target_status: 期望写入的终态状态字符串（如 ``"cancelled"`` / ``"completed"`` /
                ``"failed"``）。
            allowed_statuses: 允许执行更新的前置状态白名单；turn 当前状态不在此集合时
                不做任何修改并返回 None。
            end_reason: 可选，更新时一并写入的终态原因；为 None 时不修改该列。
            response_text: 可选，更新时一并写入的回复文本；为 None 时不修改该列。

        返回:
            更新成功时返回更新后的 TurnRecord；turn 不存在或当前状态不在允许集合内时
            返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时更新 ``turns`` 表对应行的 ``status``、``updated_at``，以及调用方
            传入的 ``end_reason`` / ``response_text`` 列（为 None 的列保持原值）。
        """

        self.get(turn_id)
        values: dict[str, object] = {
            "status": target_status,
            "updated_at": to_text(utc_now()),
        }
        if end_reason is not None:
            values["end_reason"] = end_reason
        if response_text is not None:
            values["response_text"] = response_text
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status.in_(allowed_statuses))
                .values(**values)
            )
        if not result.rowcount:
            return None
        return self.get(turn_id)

    def update_model_id(self, turn_id: str, model_id: str) -> TurnRecord:
        """回写轮次实际所用模型条目标识（运行期兜底解析修正后）。

        仅更新 ``model_id`` 与 ``updated_at``；其余字段保持不变，避免覆盖
        运行期其它并发写入（如 status）。

        参数:
            turn_id: turn 标识。
            model_id: 运行期解析得到的最终模型条目标识（``ModelEntryRecord.model_id``）。

        返回:
            更新后的 ``TurnRecord``。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``turns`` 表中对应行的 model_id 与 updated_at。
        """

        self.get(turn_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id)
                .values(model_id=model_id, updated_at=to_text(utc_now()))
            )
        return self.get(turn_id)

    def list_ids_by_task_ids(self, task_ids: list[str]) -> list[str]:
        """返回一批任务下全部 turn 的标识列表。

        只查 ``turn_id`` 一列，用于跨表级联删除等只需 id 的场景。

        参数:
            task_ids: 任务标识列表；为空时直接返回空列表。

        返回:
            匹配的 turn_id 列表；无匹配时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            task_ids 非空时打开一次主库只读 session。
        """

        if not task_ids:
            return []
        with self._session_factory() as session:
            return [
                row[0]
                for row in session.execute(
                    select(TurnModel.turn_id).where(TurnModel.task_id.in_(task_ids))
                ).all()
            ]

    def delete_by_ids(self, turn_ids: list[str]) -> None:
        """按标识批量删除 turn。

        参数:
            turn_ids: 待删除的 turn 标识列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            turn_ids 为空时直接返回；对应行不存在时静默无操作。
        """

        if not turn_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TurnModel).where(TurnModel.turn_id.in_(turn_ids)))


