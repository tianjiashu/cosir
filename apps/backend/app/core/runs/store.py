"""Durable Run State 的 SQLite 持久化仓储。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Sequence
from uuid import uuid4

from app.core.runs.records import ResumeCommandRecord, RunRecord
from app.core.runs.state_machine import RunStateMachine
from app.storage.migrations import ensure_durable_schema


class DurableRunStore:
    """读写可恢复运行状态和恢复命令。"""

    def __init__(self, database_path: Path, logger: logging.Logger) -> None:
        """初始化运行状态仓储。

        参数:
            database_path: SQLite 数据库路径。
            logger: 用于记录状态变化和异常路径的日志器。

        返回:
            无。

        异常:
            OSError: 如果数据库目录无法创建。
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            初始化 Durable Run State 相关表。
        """

        self._database_path = database_path
        self._logger = logger
        self._state_machine = RunStateMachine()
        ensure_durable_schema(database_path)

    def create_for_task(self, task_id: str, thread_id: Optional[str] = None) -> RunRecord:
        """为任务创建或返回已有运行记录。

        参数:
            task_id: 关联任务标识符。
            thread_id: 可选的 LangGraph thread_id；省略时使用新 UUID。

        返回:
            已存在或新创建的运行记录。

        异常:
            sqlite3.Error: 如果数据库写入失败。

        副作用:
            可能向 SQLite 写入一条 durable_runs 记录，并写入日志。
        """

        existing = self.get_by_task(task_id)
        if existing is not None:
            return existing

        now = _utc_now()
        run = RunRecord(
            run_id=str(uuid4()),
            task_id=task_id,
            thread_id=thread_id or str(uuid4()),
            status="created",
            wait_reason=None,
            active_step_id=None,
            active_wait_id=None,
            last_checkpoint_id=None,
            interruption_reason=None,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO durable_runs(
                    run_id, task_id, thread_id, status, wait_reason, active_step_id,
                    active_wait_id, last_checkpoint_id, interruption_reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.task_id,
                    run.thread_id,
                    run.status,
                    run.wait_reason,
                    run.active_step_id,
                    run.active_wait_id,
                    run.last_checkpoint_id,
                    run.interruption_reason,
                    _to_text(run.created_at),
                    _to_text(run.updated_at),
                ),
            )
        self._logger.info(
            "durable_run_created",
            extra={"run_id": run.run_id, "task_id": task_id},
        )
        return run

    def get(self, run_id: str) -> RunRecord:
        """按 run_id 返回运行记录。

        参数:
            run_id: 运行标识符。

        返回:
            匹配的运行记录。

        异常:
            KeyError: 如果运行记录不存在。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM durable_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_from_row(row)

    def get_by_task(self, task_id: str) -> Optional[RunRecord]:
        """按 task_id 返回运行记录。

        参数:
            task_id: 任务标识符。

        返回:
            存在时返回运行记录，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM durable_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def mark_status(
        self,
        run_id: str,
        status: str,
        wait_reason: Optional[str] = None,
        active_step_id: Optional[str] = None,
        active_wait_id: Optional[str] = None,
        interruption_reason: Optional[str] = None,
    ) -> RunRecord:
        """更新运行状态并校验状态流转。

        参数:
            run_id: 运行标识符。
            status: 目标状态。
            wait_reason: 等待原因。
            active_step_id: 当前活跃步骤标识符。
            active_wait_id: 当前等待点标识符。
            interruption_reason: 中断或复核原因。

        返回:
            更新后的运行记录。

        异常:
            KeyError: 如果运行记录不存在。
            InvalidRunTransition: 如果状态流转非法。
            sqlite3.Error: 如果更新失败。

        副作用:
            更新 SQLite 中的运行状态并写入日志。
        """

        current = self.get(run_id)
        self._state_machine.ensure_transition(current.status, status)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE durable_runs
                SET status = ?, wait_reason = ?, active_step_id = ?, active_wait_id = ?,
                    interruption_reason = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    wait_reason,
                    active_step_id,
                    active_wait_id,
                    interruption_reason,
                    _to_text(now),
                    run_id,
                ),
            )
        self._logger.info(
            "durable_run_status",
            extra={"run_id": run_id, "status": status},
        )
        return self.get(run_id)

    def list_recoverable(self) -> List[RunRecord]:
        """列出可恢复或需要处理的运行记录。

        参数:
            无。

        返回:
            状态处于 waiting、interrupted、needs_review 或 resuming 的运行记录。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM durable_runs
                WHERE status IN ('waiting', 'interrupted', 'needs_review', 'resuming')
                ORDER BY updated_at ASC
                """
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def create_resume_command(
        self,
        run_id: str,
        action: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> ResumeCommandRecord:
        """创建幂等恢复命令。

        参数:
            run_id: 被恢复的运行标识符。
            action: 恢复动作。
            payload: 恢复动作载荷。
            idempotency_key: 幂等键。

        返回:
            新建或已存在的恢复命令。

        异常:
            KeyError: 如果运行记录不存在。
            sqlite3.Error: 如果数据库写入失败。

        副作用:
            可能写入 resume_commands 表并写入日志。
        """

        self.get(run_id)
        existing = self.get_resume_command_by_key(idempotency_key)
        if existing is not None:
            self._logger.info(
                "resume_command_idempotent",
                extra={"run_id": run_id, "action": action},
            )
            return existing

        now = _utc_now()
        command = ResumeCommandRecord(
            command_id=str(uuid4()),
            run_id=run_id,
            action=action,
            payload=payload,
            idempotency_key=idempotency_key,
            status="pending",
            created_at=now,
            applied_at=None,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO resume_commands(
                        command_id, run_id, action, payload_json, idempotency_key,
                        status, created_at, applied_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.command_id,
                        command.run_id,
                        command.action,
                        json.dumps(command.payload, ensure_ascii=False, sort_keys=True),
                        command.idempotency_key,
                        command.status,
                        _to_text(command.created_at),
                        None,
                    ),
                )
        except sqlite3.IntegrityError:
            concurrent = self.get_resume_command_by_key(idempotency_key)
            if concurrent is not None:
                self._logger.info(
                    "resume_command_idempotent",
                    extra={
                        "run_id": concurrent.run_id,
                        "resume_command_id": concurrent.command_id,
                        "action": concurrent.action,
                    },
                )
                return concurrent
            raise
        self._logger.info(
            "resume_command_created",
            extra={"run_id": run_id, "action": action},
        )
        return command

    def get_resume_command_by_key(self, idempotency_key: str) -> Optional[ResumeCommandRecord]:
        """按幂等键查询恢复命令。

        参数:
            idempotency_key: 恢复命令幂等键。

        返回:
            存在时返回恢复命令，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resume_commands WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _resume_command_from_row(row) if row is not None else None

    def claim_pending_resume_commands(
        self,
        run_id: Optional[str] = None,
        actions: Optional[Sequence[str]] = None,
    ) -> List[ResumeCommandRecord]:
        """原子领取指定运行或全部运行的待处理恢复命令。

        参数:
            run_id: 可选运行标识；省略时领取所有可恢复运行的命令。
            actions: 可选动作白名单；提供时只领取指定动作。

        返回:
            本次调用成功领取、状态已切换为 processing 的命令列表。

        异常:
            sqlite3.Error: 如果查询或更新恢复命令失败。

        副作用:
            将 pending 命令原子更新为 processing，防止并发消费者重复执行副作用。
        """

        with self._connect() as connection:
            clauses = ["status = 'pending'"]
            parameters: List[str] = []
            if run_id is not None:
                clauses.append("run_id = ?")
                parameters.append(run_id)
            if actions:
                clauses.append(f"action IN ({','.join('?' for _ in actions)})")
                parameters.extend(actions)
            rows = connection.execute(
                f"SELECT * FROM resume_commands WHERE {' AND '.join(clauses)} ORDER BY created_at ASC",
                parameters,
            ).fetchall()
            claimed: List[ResumeCommandRecord] = []
            for row in rows:
                updated = connection.execute(
                    "UPDATE resume_commands SET status = 'processing' WHERE command_id = ? AND status = 'pending'",
                    (row["command_id"],),
                )
                if updated.rowcount == 1:
                    claimed.append(_resume_command_from_row(row))
        for command in claimed:
            self._logger.info(
                "resume_command_claimed",
                extra={
                    "run_id": command.run_id,
                    "resume_command_id": command.command_id,
                    "action": command.action,
                },
            )
        return claimed

    def requeue_processing_resume_commands(self, run_id: Optional[str] = None) -> int:
        """将进程中断遗留的 processing 恢复命令重新排队。

        参数:
            run_id: 可选运行标识；提供时只重排该运行的命令。

        返回:
            被重新排队的命令数量。

        异常:
            sqlite3.Error: 如果更新恢复命令失败。

        副作用:
            将未写入 applied_at 的 processing 命令恢复为 pending。
        """

        query = "UPDATE resume_commands SET status = 'pending' WHERE status = 'processing' AND applied_at IS NULL"
        parameters: tuple[str, ...] = ()
        if run_id is not None:
            query += " AND run_id = ?"
            parameters = (run_id,)
        with self._connect() as connection:
            updated = connection.execute(query, parameters)
        if updated.rowcount:
            self._logger.warning(
                "resume_commands_requeued",
                extra={"run_id": run_id or "*", "count": updated.rowcount},
            )
        return updated.rowcount

    def mark_resume_command_applied(self, command_id: str) -> ResumeCommandRecord:
        """将已成功消费的恢复命令标记为 applied。

        参数:
            command_id: 已领取恢复命令标识符。

        返回:
            更新后的恢复命令。

        异常:
            KeyError: 如果恢复命令不存在。
            sqlite3.Error: 如果更新失败。

        副作用:
            写入命令完成时间，阻止后续重复消费。
        """

        now = _utc_now()
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE resume_commands SET status = 'applied', applied_at = ? WHERE command_id = ? AND status = 'processing'",
                (_to_text(now), command_id),
            )
        if updated.rowcount != 1:
            raise KeyError(command_id)
        command = self._get_resume_command(command_id)
        self._logger.info(
            "resume_command_applied",
            extra={
                "run_id": command.run_id,
                "resume_command_id": command.command_id,
                "action": command.action,
            },
        )
        return command

    def release_resume_command(self, command_id: str) -> None:
        """释放消费失败的恢复命令，以便后续恢复流程重试。

        参数:
            command_id: 已领取但尚未完成的恢复命令标识符。

        返回:
            无。

        异常:
            sqlite3.Error: 如果更新失败。

        副作用:
            将 processing 命令还原为 pending 并记录错误恢复路径日志。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, action FROM resume_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            connection.execute(
                "UPDATE resume_commands SET status = 'pending' WHERE command_id = ? AND status = 'processing'",
                (command_id,),
            )
        self._logger.warning(
            "resume_command_released",
            extra={
                "run_id": row["run_id"] if row is not None else None,
                "resume_command_id": command_id,
                "action": row["action"] if row is not None else None,
            },
        )

    def _get_resume_command(self, command_id: str) -> ResumeCommandRecord:
        """按主键读取恢复命令内部实现。

        参数:
            command_id: 恢复命令标识符。

        返回:
            对应恢复命令记录。

        异常:
            KeyError: 如果命令不存在。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resume_commands WHERE command_id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return _resume_command_from_row(row)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开配置好的 SQLite 连接。

        参数:
            无。

        返回:
            可在 with 语句中使用的 SQLite 连接迭代器，连接启用了 Row 工厂。

        异常:
            sqlite3.Error: 如果连接失败。

        副作用:
            打开数据库连接；正常退出时提交事务，异常退出时回滚事务，最终关闭连接。
        """

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _utc_now() -> datetime:
    """返回当前 UTC 时间。

    参数:
        无。

    返回:
        当前 UTC datetime。

    异常:
        无。

    副作用:
        无。
    """

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 序列化为 SQLite 文本。

    参数:
        value: 待序列化的时间。

    返回:
        ISO-8601 字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """从 SQLite 文本解析 datetime。

    参数:
        value: ISO-8601 时间字符串。

    返回:
        解析后的 datetime。

    异常:
        ValueError: 如果字符串不是合法时间。

    副作用:
        无。
    """

    return datetime.fromisoformat(value)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    """从 SQLite 行构造运行记录。

    参数:
        row: durable_runs 查询结果行。

    返回:
        运行记录。

    异常:
        KeyError: 如果行缺少必要字段。

    副作用:
        无。
    """

    return RunRecord(
        run_id=row["run_id"],
        task_id=row["task_id"],
        thread_id=row["thread_id"],
        status=row["status"],
        wait_reason=row["wait_reason"],
        active_step_id=row["active_step_id"],
        active_wait_id=row["active_wait_id"],
        last_checkpoint_id=row["last_checkpoint_id"],
        interruption_reason=row["interruption_reason"],
        created_at=_from_text(row["created_at"]),
        updated_at=_from_text(row["updated_at"]),
    )


def _resume_command_from_row(row: sqlite3.Row) -> ResumeCommandRecord:
    """从 SQLite 行构造恢复命令记录。

    参数:
        row: resume_commands 查询结果行。

    返回:
        恢复命令记录。

    异常:
        json.JSONDecodeError: 如果载荷不是合法 JSON。

    副作用:
        无。
    """

    return ResumeCommandRecord(
        command_id=row["command_id"],
        run_id=row["run_id"],
        action=row["action"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        created_at=_from_text(row["created_at"]),
        applied_at=_from_text(row["applied_at"]) if row["applied_at"] else None,
    )
