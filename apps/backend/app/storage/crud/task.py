"""任务运行切片 CRUD。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import Select, asc, select, update

from app.events.types import EventType, RuntimeEvent
from app.storage.database import create_session_factory
from app.storage.model.task import CheckpointModel, EventModel, SessionModel, StepModel, TaskModel, TurnModel
from app.storage.records import CheckpointRecord, StepRecord, TaskRecord, TurnRecord
from app.storage.schema import initialize_app_schema


class SQLiteTaskStore:
    """持久化会话、任务、轮次、步骤与事件状态。"""

    def __init__(self, database_path: Path) -> None:
        """初始化任务存储并确保 schema 存在。

        参数:
            database_path: SQLite 数据库文件路径。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlalchemy.exc.SQLAlchemyError: 如果 schema 初始化失败。

        副作用:
            创建数据库目录、打开 SQLite engine 并创建主库表。
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create_task(self, input_text: str, status: str, session_id: Optional[str] = None, agent_id: str = "developer") -> TaskRecord:
        """在需要时创建会话，然后创建任务与轮次记录。

        参数:
            input_text: 用户提交的纯文本任务。
            session_id: 可选的、已存在的会话标识符。
            agent_id: 负责执行任务的 Agent 标识符。
            status: 新任务初始状态。

        返回:
            新创建的任务记录。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果数据库写入失败。

        副作用:
            向主库写入会话、任务与轮次记录。
        """

        now = _utc_now()
        resolved_session_id = session_id or str(uuid4())
        task = TaskRecord(str(uuid4()), resolved_session_id, agent_id, input_text, status, now, now)
        with self._session_factory.begin() as session:
            if session.get(SessionModel, resolved_session_id) is None:
                session.add(SessionModel(session_id=resolved_session_id, project_path=None, created_at=_to_text(now), updated_at=_to_text(now)))
            session.add(
                TaskModel(
                    task_id=task.task_id,
                    session_id=task.session_id,
                    agent_id=task.agent_id,
                    input_text=task.input_text,
                    status=task.status,
                    created_at=_to_text(task.created_at),
                    updated_at=_to_text(task.updated_at),
                )
            )
            session.add(
                TurnModel(
                    turn_id=str(uuid4()),
                    task_id=task.task_id,
                    input_text=input_text,
                    status=status,
                    created_at=_to_text(now),
                    updated_at=_to_text(now),
                )
            )
        return task

    def get_task(self, task_id: str) -> TaskRecord:
        """按标识符返回一个任务。

        参数:
            task_id: 待获取的任务标识符。

        返回:
            匹配的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        with self._session_factory() as session:
            row = session.get(TaskModel, task_id)
        if row is None:
            raise KeyError(task_id)
        return _task_from_model(row)

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        """更新任务状态并返回更新后的任务。

        参数:
            task_id: 待更新的任务标识符。
            status: 新的任务状态。

        返回:
            更新后的任务记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            修改主库中的任务状态。
        """

        self.get_task(task_id)
        with self._session_factory.begin() as session:
            session.execute(update(TaskModel).where(TaskModel.task_id == task_id).values(status=status, updated_at=_to_text(_utc_now())))
        return self.get_task(task_id)

    def has_status(self, task_id: str, status: str) -> bool:
        """返回任务当前是否具有某状态值。

        参数:
            task_id: 待检查的任务标识符。
            status: 需要与所存储任务状态比较的状态值。

        返回:
            当任务状态等于所给状态时为 True。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        return self.get_task(task_id).status == status

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

    def append_event(self, event: RuntimeEvent) -> None:
        """向任务时间线追加一个事件。

        参数:
            event: 待存储的运行时事件。

        返回:
            无。

        异常:
            KeyError: 如果事件所属任务不存在。

        副作用:
            将事件写入主库。
        """

        self.get_task(event.task_id)
        with self._session_factory.begin() as session:
            session.add(
                EventModel(
                    event_id=event.event_id,
                    task_id=event.task_id,
                    event_type=event.event_type.value if isinstance(event.event_type, EventType) else str(event.event_type),
                    payload_json=json.dumps(event.payload),
                    created_at=_to_text(event.created_at),
                )
            )

    def list_events(self, task_id: str) -> List[RuntimeEvent]:
        """列出一个任务的事件。

        参数:
            task_id: 需返回其事件的任务标识符。

        返回:
            任务事件的有序列表。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            rows = session.execute(_events_for_task(task_id)).scalars().all()
        return [
            RuntimeEvent(
                event_type=EventType(row.event_type),
                task_id=row.task_id,
                payload=json.loads(row.payload_json),
                event_id=row.event_id,
                created_at=_from_text(row.created_at),
            )
            for row in rows
        ]

    def create_checkpoint(self, task_id: str, stage: str, summary: str, snapshot: dict) -> CheckpointRecord:
        """为任务持久化一个状态级检查点。

        参数:
            task_id: 与检查点关联的任务标识符。
            stage: 产出该检查点的运行时阶段。
            summary: 简短的人类可读摘要。
            snapshot: 可序列化为 JSON 的运行时状态快照。

        返回:
            持久化的检查点记录。

        异常:
            KeyError: 如果任务不存在。
            TypeError: 如果快照无法序列化为 JSON。

        副作用:
            向主库写入一行检查点记录。
        """

        self.get_task(task_id)
        checkpoint = CheckpointRecord(str(uuid4()), task_id, stage, summary, snapshot, _utc_now())
        with self._session_factory.begin() as session:
            session.add(
                CheckpointModel(
                    checkpoint_id=checkpoint.checkpoint_id,
                    task_id=checkpoint.task_id,
                    stage=checkpoint.stage,
                    summary=checkpoint.summary,
                    snapshot_json=json.dumps(checkpoint.snapshot),
                    created_at=_to_text(checkpoint.created_at),
                )
            )
        return checkpoint

    def list_checkpoints(self, task_id: str) -> List[CheckpointRecord]:
        """列出一个任务的持久化检查点。

        参数:
            task_id: 需返回其检查点的任务标识符。

        返回:
            与任务关联的有序检查点记录。

        异常:
            KeyError: 如果任务不存在。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._session_factory() as session:
            rows = session.execute(
                select(CheckpointModel).where(CheckpointModel.task_id == task_id).order_by(asc(CheckpointModel.created_at), asc(CheckpointModel.checkpoint_id))
            ).scalars().all()
        return [_checkpoint_from_model(row) for row in rows]


def _utc_now() -> datetime:
    """返回当前 UTC datetime。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 值序列化为 ISO-8601 文本。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 ISO-8601 datetime 字符串。"""

    return datetime.fromisoformat(value)


def _events_for_task(task_id: str) -> Select[tuple[EventModel]]:
    """构造任务事件查询语句。"""

    return select(EventModel).where(EventModel.task_id == task_id).order_by(asc(EventModel.created_at), asc(EventModel.event_id))


def _task_from_model(row: TaskModel) -> TaskRecord:
    """将任务 model 转换为记录。"""

    return TaskRecord(row.task_id, row.session_id, row.agent_id, row.input_text, row.status, _from_text(row.created_at), _from_text(row.updated_at))


def _turn_from_model(row: TurnModel) -> TurnRecord:
    """将轮次 model 转换为记录。"""

    return TurnRecord(row.turn_id, row.task_id, row.input_text, row.status, _from_text(row.created_at), _from_text(row.updated_at))


def _step_model(step: StepRecord) -> StepModel:
    """将步骤记录转换为 model。"""

    return StepModel(
        step_id=step.step_id,
        turn_id=step.turn_id,
        step_type=step.step_type,
        status=step.status,
        input_summary=step.input_summary,
        output_summary=step.output_summary,
        error=step.error,
        created_at=_to_text(step.created_at),
        updated_at=_to_text(step.updated_at),
    )


def _step_from_model(row: StepModel) -> StepRecord:
    """将步骤 model 转换为记录。"""

    return StepRecord(row.step_id, row.turn_id, row.step_type, row.status, row.input_summary, row.output_summary, row.error, _from_text(row.created_at), _from_text(row.updated_at))


def _checkpoint_from_model(row: CheckpointModel) -> CheckpointRecord:
    """将 checkpoint model 转换为记录。"""

    return CheckpointRecord(row.checkpoint_id, row.task_id, row.stage, row.summary, json.loads(row.snapshot_json), _from_text(row.created_at))
