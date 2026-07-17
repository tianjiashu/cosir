"""StepStoreMixin implementation for SQLiteTaskStore."""

from app.storage.crud.task_common import *


class StepStoreMixin:
    """SQLiteTaskStore StepStoreMixin responsibilities."""

    def create_step(
        self,
        turn_id: str,
        step_type: str,
        status: str,
        input_summary: str = "",
        output_summary: str = "",
        error: Optional[str] = None,
    ) -> StepRecord:
        """创建一个持久化的运行时步骤。

        参数:
            turn_id: 与该步骤关联的轮次标识符。
            step_type: 运行时步骤类型。
            status: 初始步骤状态。
            input_summary: 简短的诊断输入摘要。
            output_summary: 简短的诊断输出摘要。
            error: 可选的错误消息。

        返回:
            已创建的步骤记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果插入失败。

        副作用:
            向主库写入一行步骤记录。
        """

        now = _utc_now()
        step = StepRecord(str(uuid4()), turn_id, step_type, status, input_summary, output_summary, error, now, now)
        with self._session_factory.begin() as session:
            session.add(_step_model(step))
        return step

    def update_step_status(self, step_id: str, status: str, output_summary: str = "", error: Optional[str] = None) -> StepRecord:
        """更新一个持久化的运行时步骤状态。

        参数:
            step_id: 待更新的步骤标识符。
            status: 新的步骤状态。
            output_summary: 简短的诊断输出摘要。
            error: 用于失败步骤的可选错误消息。

        返回:
            更新后的步骤记录。

        异常:
            KeyError: 如果步骤不存在。

        副作用:
            修改主库中的步骤行。
        """

        with self._session_factory.begin() as session:
            row = session.get(StepModel, step_id)
            if row is None:
                raise KeyError(step_id)
            row.status = status
            row.output_summary = output_summary
            row.error = error
            row.updated_at = _to_text(_utc_now())
        with self._session_factory() as session:
            refreshed = session.get(StepModel, step_id)
        if refreshed is None:
            raise KeyError(step_id)
        return _step_from_model(refreshed)

    def list_steps_for_task(self, task_id: str) -> List[StepRecord]:
        """列出一个任务的持久化运行时步骤。

        参数:
            task_id: 需返回其步骤的任务标识符。

        返回:
            与任务关联的有序步骤记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            rows = session.execute(
                select(StepModel)
                .join(TurnModel, TurnModel.turn_id == StepModel.turn_id)
                .where(TurnModel.task_id == task_id)
                .order_by(asc(StepModel.created_at), asc(StepModel.step_id))
            ).scalars().all()
        return [_step_from_model(row) for row in rows]

    def update_steps_status_for_task(self, task_id: str, current_status: str, status: str, error: str) -> int:
        """按当前状态批量更新任务步骤。

        参数:
            task_id: 需更新其步骤的任务标识符。
            current_status: 需要匹配的当前步骤状态。
            status: 应用于匹配步骤的目标状态。
            error: 需要持久化到每个被更新步骤上的错误/终态原因。

        返回:
            被更新的步骤行数。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改主库中匹配条件的步骤行。
        """

        self.get_task(task_id)
        now_text = _to_text(_utc_now())
        with self._session_factory.begin() as session:
            turn_ids = select(TurnModel.turn_id).where(TurnModel.task_id == task_id)
            result = session.execute(
                update(StepModel)
                .where(StepModel.status == current_status, StepModel.turn_id.in_(turn_ids))
                .values(status=status, error=error, updated_at=now_text)
            )
        return result.rowcount or 0
