"""基于 SQLite 的任务、步骤与事件存储。"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from app.events.types import EventType, RuntimeEvent
from app.storage.records import (
    CheckpointRecord,
    SessionRecord,
    StepRecord,
    TaskRecord,
    TurnRecord,
)


class SQLiteTaskStore:
    """在 SQLite 中持久化会话、任务、轮次、步骤与事件状态。"""

    def __init__(self, database_path: Path) -> None:
        """初始化 SQLite 存储并确保表结构存在。

        参数:
            database_path: SQLite 数据库文件路径。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法被创建。
            sqlite3.Error: 如果表结构创建失败。

        副作用:
            创建数据库目录、打开 SQLite 连接并创建表。
        """

        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def create_task(
        self,
        input_text: str,
        session_id: Optional[str] = None,
        agent_id: str = "developer",
    ) -> TaskRecord:
        """在需要时创建会话，然后创建任务与轮次记录。

        参数:
            input_text: 用户提交的纯文本任务。
            session_id: 可选的、已存在的会话标识符。
            agent_id: 负责执行任务的 Agent 标识符。

        返回:
            新创建的任务记录。

        异常:
            ValueError: 如果输入文本为空。
            sqlite3.Error: 如果数据库写入失败。

        副作用:
            向 SQLite 写入会话、任务与轮次记录。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")

        now = _utc_now()
        resolved_session_id = session_id or str(uuid4())
        task = TaskRecord(
            task_id=str(uuid4()),
            session_id=resolved_session_id,
            agent_id=agent_id,
            input_text=input_text,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sessions(session_id, project_path, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (resolved_session_id, None, _to_text(now), _to_text(now)),
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, session_id, agent_id, input_text, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.session_id,
                    task.agent_id,
                    task.input_text,
                    task.status,
                    _to_text(task.created_at),
                    _to_text(task.updated_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO turns(turn_id, task_id, input_text, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    task.task_id,
                    input_text,
                    "pending",
                    _to_text(now),
                    _to_text(now),
                ),
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
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT task_id, session_id, agent_id, input_text, status, created_at, updated_at
                FROM tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskRecord(
            task_id=row["task_id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            input_text=row["input_text"],
            status=row["status"],
            created_at=_from_text(row["created_at"]),
            updated_at=_from_text(row["updated_at"]),
        )

    def update_status(self, task_id: str, status: str) -> TaskRecord:
        """更新任务状态并返回更新后的任务。

        参数:
            task_id: 待更新的任务标识符。
            status: 新的任务状态。

        返回:
            更新后的任务记录。

        异常:
            KeyError: 如果任务不存在。
            sqlite3.Error: 如果更新失败。

        副作用:
            修改 SQLite 中的任务状态。
        """

        self.get_task(task_id)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, _to_text(now), task_id),
            )
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
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT turn_id, task_id, input_text, status, created_at, updated_at
                FROM turns
                WHERE task_id = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TurnRecord(
            turn_id=row["turn_id"],
            task_id=row["task_id"],
            input_text=row["input_text"],
            status=row["status"],
            created_at=_from_text(row["created_at"]),
            updated_at=_from_text(row["updated_at"]),
        )

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
            sqlite3.Error: 如果插入失败。

        副作用:
            向 SQLite 写入一行步骤记录。
        """

        now = _utc_now()
        step = StepRecord(
            step_id=str(uuid4()),
            turn_id=turn_id,
            step_type=step_type,
            status=status,
            input_summary=input_summary,
            output_summary=output_summary,
            error=error,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO steps(
                    step_id, turn_id, step_type, status, input_summary,
                    output_summary, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.step_id,
                    step.turn_id,
                    step.step_type,
                    step.status,
                    step.input_summary,
                    step.output_summary,
                    step.error,
                    _to_text(step.created_at),
                    _to_text(step.updated_at),
                ),
            )
        return step

    def update_step_status(
        self,
        step_id: str,
        status: str,
        output_summary: str = "",
        error: Optional[str] = None,
    ) -> StepRecord:
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
            sqlite3.Error: 如果更新失败。

        副作用:
            修改 SQLite 中的步骤行。
        """

        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE steps
                SET status = ?, output_summary = ?, error = ?, updated_at = ?
                WHERE step_id = ?
                """,
                (status, output_summary, error, _to_text(now), step_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(step_id)
            row = connection.execute(
                """
                SELECT
                    step_id,
                    turn_id,
                    step_type,
                    status,
                    input_summary,
                    output_summary,
                    error,
                    created_at,
                    updated_at
                FROM steps
                WHERE step_id = ?
                """,
                (step_id,),
            ).fetchone()
        if row is None:
            raise KeyError(step_id)
        return _step_from_row(row)

    def list_steps_for_task(self, task_id: str) -> List[StepRecord]:
        """列出一个任务的持久化运行时步骤。

        参数:
            task_id: 需返回其步骤的任务标识符。

        返回:
            与任务关联的有序步骤记录。

        异常:
            KeyError: 如果任务不存在。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    steps.step_id,
                    steps.turn_id,
                    steps.step_type,
                    steps.status,
                    steps.input_summary,
                    steps.output_summary,
                    steps.error,
                    steps.created_at,
                    steps.updated_at
                FROM steps
                INNER JOIN turns ON turns.turn_id = steps.turn_id
                WHERE turns.task_id = ?
                ORDER BY steps.created_at ASC, steps.rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_step_from_row(row) for row in rows]

    def close_running_steps_for_task(
        self,
        task_id: str,
        status: str,
        error: str,
    ) -> int:
        """关闭与一个任务关联的所有运行中步骤。

        参数:
            task_id: 需关闭其运行中步骤的任务标识符。
            status: 应用于运行中步骤的终态状态。
            error: 需要持久化到每个被更新步骤上的错误/终态原因。

        返回:
            被更新的步骤行数。

        异常:
            KeyError: 如果任务不存在。
            sqlite3.Error: 如果更新失败。

        副作用:
            修改 SQLite 中运行中的步骤行。
        """

        self.get_task(task_id)
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE steps
                SET status = ?, error = ?, updated_at = ?
                WHERE status = 'running'
                  AND turn_id IN (
                      SELECT turn_id FROM turns WHERE task_id = ?
                  )
                """,
                (status, error, _to_text(now), task_id),
            )
            return cursor.rowcount

    def append_event(self, event: RuntimeEvent) -> None:
        """向任务时间线追加一个事件。

        参数:
            event: 待存储的运行时事件。

        返回:
            无。

        异常:
            KeyError: 如果事件所属任务不存在。
            sqlite3.Error: 如果插入失败。

        副作用:
            将事件写入 SQLite。
        """

        self.get_task(event.task_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(event_id, task_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.event_type,
                    json.dumps(event.payload),
                    _to_text(event.created_at),
                ),
            )

    def list_events(self, task_id: str) -> List[RuntimeEvent]:
        """列出一个任务的事件。

        参数:
            task_id: 需返回其事件的任务标识符。

        返回:
            任务事件的有序列表。

        异常:
            KeyError: 如果任务不存在。
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, task_id, event_type, payload_json, created_at
                FROM events
                WHERE task_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            RuntimeEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                event_type=EventType(row["event_type"]),
                payload=json.loads(row["payload_json"]),
                created_at=_from_text(row["created_at"]),
            )
            for row in rows
        ]

    def create_checkpoint(
        self,
        task_id: str,
        stage: str,
        summary: str,
        snapshot: dict,
    ) -> CheckpointRecord:
        """为任务持久化一个状态级检查点。

        参数:
            task_id: 与检查点关联的任务标识符。
            stage: 产出该检查点的运行时阶段。
            summary: 简短的、人类可读的检查点摘要。
            snapshot: 可序列化为 JSON 的运行时状态快照。

        返回:
            持久化的检查点记录。

        异常:
            KeyError: 如果任务不存在。
            TypeError: 如果快照无法被序列化为 JSON。
            sqlite3.Error: 如果插入失败。

        副作用:
            向 SQLite 写入一行检查点记录。
        """

        self.get_task(task_id)
        now = _utc_now()
        checkpoint = CheckpointRecord(
            checkpoint_id=str(uuid4()),
            task_id=task_id,
            stage=stage,
            summary=summary,
            snapshot=snapshot,
            created_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, task_id, stage, summary, snapshot_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.task_id,
                    checkpoint.stage,
                    checkpoint.summary,
                    json.dumps(checkpoint.snapshot),
                    _to_text(checkpoint.created_at),
                ),
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
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        self.get_task(task_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT checkpoint_id, task_id, stage, summary, snapshot_json, created_at
                FROM checkpoints
                WHERE task_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (task_id,),
            ).fetchall()
        return [_checkpoint_from_row(row) for row in rows]

    def _initialize_schema(self) -> None:
        """创建第一版运行时切片所需的 SQLite 表。

        参数:
            无。

        返回:
            无。

        异常:
            sqlite3.Error: 如果表结构创建失败。

        副作用:
            在配置好的 SQLite 数据库中创建表。
        """

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'developer',
                    input_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS steps (
                    step_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    step_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_summary TEXT NOT NULL,
                    output_summary TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES turns(turn_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                """
            )
            _ensure_column(
                connection=connection,
                table_name="tasks",
                column_name="agent_id",
                definition="agent_id TEXT NOT NULL DEFAULT 'developer'",
            )

    def _connect(self) -> sqlite3.Connection:
        """打开一个为行访问配置好的 SQLite 连接。

        参数:
            无。

        返回:
            启用了 row factory 的 SQLite 连接。

        异常:
            sqlite3.Error: 如果数据库无法被打开。

        副作用:
            打开一个 SQLite 数据库连接。
        """

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as wal_error:
            # WAL 不可用时回退到默认 rollback journal，不阻断正常连接。
            logging.getLogger("coding_agent.backend").warning(
                "sqlite_wal_unavailable path=%s error=%s",
                self._database_path,
                wal_error,
            )
        return connection


def _utc_now() -> datetime:
    """返回当前的 UTC datetime。

    参数:
        无。

    返回:
        带时区信息的当前 UTC datetime。

    异常:
        无。

    副作用:
        读取系统时钟。
    """

    return datetime.now(timezone.utc)


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    """当已有表缺少某列时为其添加该列。

    参数:
        connection: 已打开的 SQLite 连接。
        table_name: 需要检查其列的表。
        column_name: 需要保证存在的列名。
        definition: 用于 ``ALTER TABLE`` 的 SQL 列定义。

    返回:
        无。

    异常:
        sqlite3.Error: 如果表结构检查或修改失败。

    副作用:
        可能修改 SQLite 表结构。
    """

    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_columns = {row["name"] for row in rows}
    if column_name not in existing_columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")


def _to_text(value: datetime) -> str:
    """将 datetime 值序列化以便 SQLite 存储。

    参数:
        value: 待序列化的 datetime 值。

    返回:
        ISO-8601 datetime 字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 SQLite datetime 字符串。

    参数:
        value: ISO-8601 datetime 字符串。

    返回:
        解析后的 datetime 值。

    异常:
        ValueError: 如果值不是合法的 ISO-8601 datetime。

    副作用:
        无。
    """

    return datetime.fromisoformat(value)


def _step_from_row(row: sqlite3.Row) -> StepRecord:
    """将 SQLite 行转换为 StepRecord。

    参数:
        row: 包含所有步骤列的 SQLite 行。

    返回:
        从行填充得到的 StepRecord。

    异常:
        ValueError: 如果时间戳字段无法被解析。

    副作用:
        无。
    """

    return StepRecord(
        step_id=row["step_id"],
        turn_id=row["turn_id"],
        step_type=row["step_type"],
        status=row["status"],
        input_summary=row["input_summary"],
        output_summary=row["output_summary"],
        error=row["error"],
        created_at=_from_text(row["created_at"]),
        updated_at=_from_text(row["updated_at"]),
    )


def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointRecord:
    """将 SQLite 行转换为 CheckpointRecord。

    参数:
        row: 包含所有检查点列的 SQLite 行。

    返回:
        从行填充得到的 CheckpointRecord。

    异常:
        ValueError: 如果时间戳或 JSON 字段无法被解析。

    副作用:
        无。
    """

    return CheckpointRecord(
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        stage=row["stage"],
        summary=row["summary"],
        snapshot=json.loads(row["snapshot_json"]),
        created_at=_from_text(row["created_at"]),
    )
