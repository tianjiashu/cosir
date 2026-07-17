"""CheckpointStoreMixin implementation for SQLiteTaskStore."""

from app.storage.crud.task_common import *


class CheckpointStoreMixin:
    """SQLiteTaskStore CheckpointStoreMixin responsibilities."""

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
