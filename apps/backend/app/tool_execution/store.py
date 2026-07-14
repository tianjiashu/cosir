"""工具调用和执行记录 SQLite 仓储。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, Optional
from uuid import uuid4

from app.storage.migrations import ensure_durable_schema
from app.tool_execution.records import ToolCallRecord, ToolExecutionRecord


class ToolExecutionStore:
    """读写工具调用和执行记录。"""

    def __init__(self, database_path: Path) -> None:
        """初始化工具执行仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            初始化工具执行相关表。
        """

        self._database_path = database_path
        ensure_durable_schema(database_path)

    def create_tool_call(
        self,
        run_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        permission: str,
        idempotency_key: str,
        step_id: Optional[str] = None,
    ) -> ToolCallRecord:
        """创建幂等工具调用记录。

        参数:
            run_id: 所属运行标识符。
            tool_name: 工具名称。
            arguments: 工具参数。
            permission: 权限级别。
            idempotency_key: 幂等键。
            step_id: 可选步骤标识符。

        返回:
            新建或已有工具调用记录。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            可能写入 tool_calls 表。
        """

        existing = self.get_tool_call_by_key(idempotency_key)
        if existing is not None:
            return existing
        now = _utc_now()
        call = ToolCallRecord(
            tool_call_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            arguments=arguments,
            permission=permission,
            status="requested",
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO tool_calls(
                        tool_call_id, run_id, step_id, tool_name, arguments_json,
                        permission, status, idempotency_key, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        call.tool_call_id,
                        call.run_id,
                        call.step_id,
                        call.tool_name,
                        json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                        call.permission,
                        call.status,
                        call.idempotency_key,
                        _to_text(call.created_at),
                        _to_text(call.updated_at),
                    ),
                )
        except sqlite3.IntegrityError:
            concurrent = self.get_tool_call_by_key(idempotency_key)
            if concurrent is not None:
                return concurrent
            raise
        return call

    def update_tool_call_status(self, tool_call_id: str, status: str) -> ToolCallRecord:
        """更新工具调用状态。

        参数:
            tool_call_id: 工具调用标识符。
            status: 目标状态。

        返回:
            更新后的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。
            sqlite3.Error: 如果更新失败。

        副作用:
            更新 tool_calls 表。
        """

        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool_calls SET status = ?, updated_at = ? WHERE tool_call_id = ?",
                (status, _to_text(now), tool_call_id),
            )
        if cursor.rowcount == 0:
            raise KeyError(tool_call_id)
        return self.get_tool_call(tool_call_id)

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
        """按标识符返回工具调用记录。

        参数:
            tool_call_id: 工具调用标识符。

        返回:
            匹配的工具调用记录。

        异常:
            KeyError: 如果工具调用不存在。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
        if row is None:
            raise KeyError(tool_call_id)
        return _tool_call_from_row(row)

    def get_tool_call_by_key(self, idempotency_key: str) -> Optional[ToolCallRecord]:
        """按幂等键查询工具调用记录。

        参数:
            idempotency_key: 工具调用幂等键。

        返回:
            存在时返回工具调用记录，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_calls WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _tool_call_from_row(row) if row is not None else None

    def create_execution(
        self,
        tool_call_id: str,
        status: str,
        effect_status: str,
        artifact_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> ToolExecutionRecord:
        """创建工具执行记录。

        参数:
            tool_call_id: 关联工具调用标识符。
            status: 执行状态。
            effect_status: 副作用状态。
            artifact_id: 可选 artifact 标识符。
            error: 可选错误信息。

        返回:
            新建工具执行记录。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 tool_executions 表。
        """

        now = _utc_now()
        record = ToolExecutionRecord(
            execution_id=str(uuid4()),
            tool_call_id=tool_call_id,
            status=status,
            effect_status=effect_status,
            artifact_id=artifact_id,
            error=error,
            started_at=now,
            completed_at=now if status in {"succeeded", "failed", "cancelled", "timed_out"} else None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_executions(
                    execution_id, tool_call_id, status, effect_status, artifact_id,
                    error, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.execution_id,
                    record.tool_call_id,
                    record.status,
                    record.effect_status,
                    record.artifact_id,
                    record.error,
                    _to_text(record.started_at),
                    _to_text(record.completed_at) if record.completed_at else None,
                ),
            )
        return record

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开工具执行仓储使用的 SQLite 连接。

        参数:
            无。

        返回:
            可在 with 语句中使用的 SQLite 连接迭代器，连接启用了 Row 工厂。

        异常:
            sqlite3.Error: 如果数据库连接失败。

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
    """返回工具执行仓储使用的当前 UTC 时间。

    参数:
        无。

    返回:
        带 UTC 时区的当前 datetime。

    异常:
        无。

    副作用:
        无。
    """

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将工具执行时间序列化为 SQLite 文本。

    参数:
        value: 待序列化的 datetime。

    返回:
        ISO-8601 格式的时间字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """将 SQLite 时间文本解析为 datetime。

    参数:
        value: ISO-8601 格式的时间字符串。

    返回:
        解析后的 datetime。

    异常:
        ValueError: 如果 value 不是合法的 ISO-8601 时间文本。

    副作用:
        无。
    """

    return datetime.fromisoformat(value)


def _tool_call_from_row(row: sqlite3.Row) -> ToolCallRecord:
    """从 SQLite 行构造工具调用领域记录。

    参数:
        row: tool_calls 查询返回的 SQLite 行。

    返回:
        反序列化后的工具调用记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        json.JSONDecodeError: 如果 arguments_json 不是合法 JSON。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return ToolCallRecord(
        tool_call_id=row["tool_call_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]),
        permission=row["permission"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        created_at=_from_text(row["created_at"]),
        updated_at=_from_text(row["updated_at"]),
    )
