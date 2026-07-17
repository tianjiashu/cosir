"""TurnStoreMixin implementation for SQLiteTaskStore."""

from app.storage.crud.task_common import *


class TurnStoreMixin:
    """SQLiteTaskStore TurnStoreMixin responsibilities."""

    def create_turn(self, task_id: str, input_text: str, status: str = "pending") -> TurnRecord:
        """为已有任务追加一个待运行轮次。

        参数:
            task_id: 目标任务容器标识符。
            input_text: 本轮用户输入文本。
            status: 新轮次初始状态。

        返回:
            已创建的轮次记录。

        异常:
            KeyError: 如果任务不存在。
            ValueError: 如果输入文本为空。

        副作用:
            写入 turns 表，并更新 task 的最近摘要与最新 turn。
        """

        if not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        self.get_task(task_id)
        now = _utc_now()
        turn = TurnRecord(str(uuid4()), task_id, input_text, status, now, now)
        with self._session_factory.begin() as session:
            session.add(
                TurnModel(
                    turn_id=turn.turn_id,
                    task_id=turn.task_id,
                    input_text=turn.input_text,
                    status=turn.status,
                    created_at=_to_text(turn.created_at),
                    updated_at=_to_text(turn.updated_at),
                )
            )
            session.execute(
                update(TaskModel)
                .where(TaskModel.task_id == task_id)
                .values(
                    latest_turn_id=turn.turn_id,
                    last_message_preview=_preview(input_text),
                    updated_at=_to_text(now),
                )
            )
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        """按标识符返回一个轮次。

        参数:
            turn_id: 待获取的轮次标识符。

        返回:
            匹配的轮次记录。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            无。
        """

        with self._session_factory() as session:
            row = session.get(TurnModel, turn_id)
        if row is None:
            raise KeyError(turn_id)
        return _turn_from_model(row)

    def list_turns_for_task(self, task_id: str) -> List[TurnRecord]:
        """列出指定任务下的所有轮次。

        参数:
            task_id: 待查询的任务标识符。

        返回:
            轮次记录列表，按创建时间排序。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            rows = session.execute(
                select(TurnModel).where(TurnModel.task_id == task_id).order_by(asc(TurnModel.created_at), asc(TurnModel.turn_id))
            ).scalars().all()
        return [_turn_from_model(row) for row in rows]

    def update_turn_status(self, turn_id: str, status: str) -> TurnRecord:
        """更新轮次状态并返回更新后的轮次。

        参数:
            turn_id: 待更新的轮次标识符。
            status: 新的轮次状态。

        返回:
            更新后的轮次记录。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            修改主库中的轮次状态。
        """

        self.get_turn(turn_id)
        with self._session_factory.begin() as session:
            session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id)
                .values(status=status, updated_at=_to_text(_utc_now()))
            )
        return self.get_turn(turn_id)

    def claim_pending_turn(self, turn_id: str) -> bool:
        """将 pending 轮次原子推进为 running。

        参数:
            turn_id: 待领取运行权的轮次标识符。

        返回:
            当本次调用成功从 pending 改为 running 时为 True；如果轮次已被其他
            stream 领取或已处于终态，则返回 False。

        异常:
            KeyError: 如果轮次不存在。

        副作用:
            在主库中按状态条件更新 turns 表。
        """

        self.get_turn(turn_id)
        now_text = _to_text(_utc_now())
        with self._session_factory.begin() as session:
            result = session.execute(
                update(TurnModel)
                .where(TurnModel.turn_id == turn_id, TurnModel.status == "pending")
                .values(status="running", updated_at=now_text)
            )
        return bool(result.rowcount)

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        """返回与任务关联的第一个轮次。

        参数:
            task_id: 需返回其轮次的任务标识符。

        返回:
            与任务关联的轮次记录。

        异常:
            KeyError: 如果任务或轮次不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            row = session.execute(
                select(TurnModel).where(TurnModel.task_id == task_id).order_by(asc(TurnModel.created_at)).limit(1)
            ).scalar_one_or_none()
        if row is None:
            raise KeyError(task_id)
        return _turn_from_model(row)
