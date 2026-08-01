"""``turns`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``turns`` 单表的增删改查与 model↔record 转换。

职责边界：
- 负责：turn 单表读写、``TurnModel``↔``TurnRecord`` 转换。
- 不负责：跨表操作与任务编排（由 ``service/task/`` 负责）、业务规则。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

from uuid import uuid4

from sqlalchemy import asc, select, update

from app.models import TurnRecord
from app.storage.model.turn_model import TurnModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import from_text, to_text, utc_now


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
    ) -> TurnRecord:
        """新建一条 turn 记录并落库。

        ``turn_id`` 由本方法生成（UUID4），创建 / 更新时间以当前 UTC 时间统一填充。

        参数:
            task_id: 所属任务标识。
            input_text: 本轮输入文本；不能为空白。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本次轮次绑定的 agent 标识；为 None 时表示回退到
                所属任务的 ``agent_id`` 默认归属（由运行时解析）。

        返回:
            落库成功的 ``TurnRecord``。

        异常:
            ValueError: 如果 input_text 去除首尾空白后为空。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``turns`` 表插入一行。
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
            response_text=None,
            agent_id=agent_id,
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
                    agent_id=turn.agent_id,
                    created_at=to_text(turn.created_at),
                    updated_at=to_text(turn.updated_at),
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
            row = session.get(TurnModel, turn_id)
        if row is None:
            raise KeyError(turn_id)
        return self._turn_from_model(row)

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
        return [self._turn_from_model(row) for row in rows]

    def update_status(self, turn_id: str, status: str, end_reason: str | None = None) -> TurnRecord:
        """更新 turn 状态并刷新更新时间。

        先校验 turn 存在（不存在则抛出），再更新状态与 ``updated_at``；``end_reason``
        用于承载终态（cancelled / failed）的原因，缺省时保持原值（仅当新值非空才覆盖，
        避免把已有原因清空为 None）。

        参数:
            turn_id: turn 标识。
            status: 新状态值。
            end_reason: 可选的终态原因；传入非 None 时覆盖，否则保留原值。

        返回:
            更新后的 ``TurnRecord``。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``turns`` 表中对应行的 status / end_reason 与 updated_at。
        """

        self.get(turn_id)
        with self._session_factory.begin() as session:
            values = {"status": status, "updated_at": to_text(utc_now())}
            if end_reason is not None:
                values["end_reason"] = end_reason
            session.execute(update(TurnModel).where(TurnModel.turn_id == turn_id).values(**values))
        return self.get(turn_id)

    def cancel_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """以原子方式把 active turn 取消。

        仅当 turn 当前仍处于 ``pending`` 或 ``running`` 时，才更新为 ``cancelled``。
        该条件更新用于避免取消请求和完成/失败收尾竞态时改写历史终态。

        参数:
            turn_id: turn 标识。
            end_reason: 取消原因。

        返回:
            成功取消时返回更新后的 TurnRecord；turn 存在但已是其它状态时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时更新 ``turns`` 表中对应行的 status / end_reason / updated_at。
        """

        self.get(turn_id)
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status.in_(("pending", "running")))
                .values(
                    status="cancelled",
                    end_reason=end_reason,
                    updated_at=to_text(utc_now()),
                )
            )
        if not result.rowcount:
            return None
        return self.get(turn_id)

    def complete_if_running(self, turn_id: str, response_text: str) -> TurnRecord | None:
        """以原子方式把 running turn 完成为 completed 并写入回复文本。

        参数:
            turn_id: turn 标识。
            response_text: Agent 最终回复文本。

        返回:
            成功完成时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时同事务更新 ``status``、``response_text`` 和 ``updated_at``。
        """

        self.get(turn_id)
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status == "running")
                .values(
                    status="completed",
                    response_text=response_text,
                    updated_at=to_text(utc_now()),
                )
            )
        if not result.rowcount:
            return None
        return self.get(turn_id)

    def fail_if_running(self, turn_id: str, end_reason: str | None = None) -> TurnRecord | None:
        """以原子方式把 running turn 标记为 failed。

        参数:
            turn_id: turn 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时更新 ``status``、``end_reason`` 和 ``updated_at``。
        """

        self.get(turn_id)
        values = {"status": "failed", "updated_at": to_text(utc_now())}
        if end_reason is not None:
            values["end_reason"] = end_reason
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status == "running")
                .values(**values)
            )
        if not result.rowcount:
            return None
        return self.get(turn_id)

    def update_response(self, turn_id: str, response_text: str | None) -> TurnRecord:
        """更新轮次的 Agent 回复文本并刷新更新时间。

        在轮次进入 ``completed`` 终态时调用，把本轮 Agent 的最终回复落库，供历史对话接口
        （``GET /tasks/{task_id}/turns``）直接返回，避免回放 checkpoint。

        参数:
            turn_id: turn 标识。
            response_text: Agent 的最终回复文本；为 None 时清空（极少用）。

        返回:
            更新后的 ``TurnRecord``。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``turns`` 表中对应行的 response_text 与 updated_at。
        """

        self.get(turn_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id)
                .values(response_text=response_text, updated_at=to_text(utc_now()))
            )
        return self.get(turn_id)

    def claim_pending(self, turn_id: str) -> bool:
        """以原子方式把处于 ``pending`` 的 turn 抢占为 ``running``。

        利用 ``WHERE status='pending'`` 的条件更新实现乐观并发抢占：仅当该 turn 仍为 pending
        时才更新成功，用于避免多个消费者重复执行同一 turn。

        参数:
            turn_id: turn 标识。

        返回:
            抢占成功（本次确实把 pending 更新为 running）返回 True；turn 已被他人抢占或非
            pending 状态返回 False。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            条件满足时更新 ``turns`` 表中对应行的 status 与 updated_at。
        """

        self.get(turn_id)
        now_text = to_text(utc_now())
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status == "pending")
                .values(status="running", updated_at=now_text)
            )
        return bool(result.rowcount)

    def get_first_for_task(self, task_id: str) -> TurnRecord:
        """返回某任务下创建时间最早的 turn。

        参数:
            task_id: 任务标识。

        返回:
            该任务最早创建的 ``TurnRecord``。

        异常:
            KeyError: 如果该任务下没有任何 turn。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = session.execute(
                select(TurnModel)
                .where(TurnModel.task_id == task_id)
                .order_by(asc(TurnModel.created_at))
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            raise KeyError(task_id)
        return self._turn_from_model(row)

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        """返回某任务下创建时间最新的 turn（跨轮继续对话的当前轮）。

        参数:
            task_id: 任务标识。

        返回:
            该任务最新创建的 ``TurnRecord``。

        异常:
            KeyError: 如果该任务下没有任何 turn。
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            row = session.execute(
                select(TurnModel)
                .where(TurnModel.task_id == task_id)
                .order_by(TurnModel.created_at.desc(), TurnModel.turn_id.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            raise KeyError(task_id)
        return self._turn_from_model(row)

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

        from sqlalchemy import select

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
            turn_ids 非空时从 ``turns`` 表删除匹配的行。
        """

        from sqlalchemy import delete

        if not turn_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(delete(TurnModel).where(TurnModel.turn_id.in_(turn_ids)))

    def _turn_from_model(self, row: TurnModel) -> TurnRecord:
        """把 ``TurnModel`` ORM 行转换为业务 ``TurnRecord``。

        转换过程把库中存储的文本时间戳还原为 datetime。

        参数:
            row: 查询得到的 ``TurnModel`` 行。

        返回:
            对应的 ``TurnRecord``。

        异常:
            无。

        副作用:
            无。
        """
        return TurnRecord(
            row.turn_id,
            row.task_id,
            row.input_text,
            row.status,
            from_text(row.created_at),
            from_text(row.updated_at),
            row.end_reason,
            row.response_text,
            row.agent_id,
        )
