"""Human-in-loop 请求 SQLite 仓储。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

from app.domain.human_input.records import HumanInputRequestRecord, HumanInputResponseRecord
from app.storage.migrations import ensure_durable_schema


class HumanInputStore:
    """读写通用人工输入请求和响应。"""

    def __init__(self, database_path: Path) -> None:
        """初始化人工输入仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            初始化 human input 相关表。
        """

        self._database_path = database_path
        ensure_durable_schema(database_path)

    def create_request(
        self,
        run_id: str,
        prompt: str,
        schema: Dict[str, Any],
        step_id: Optional[str] = None,
    ) -> HumanInputRequestRecord:
        """创建人工输入请求。

        参数:
            run_id: 运行标识符。
            prompt: 展示给用户的问题。
            schema: 期望响应结构。
            step_id: 可选步骤标识符。

        返回:
            新建人工输入请求。

        异常:
            ValueError: 如果 prompt 为空。
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 human_input_requests 表。
        """

        if not prompt.strip():
            raise ValueError("prompt must not be blank")
        now = _utc_now()
        request = HumanInputRequestRecord(
            request_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            prompt=prompt,
            schema=schema,
            status="pending",
            created_at=now,
            responded_at=None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO human_input_requests(
                    request_id, run_id, step_id, prompt, schema_json,
                    status, created_at, responded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.run_id,
                    request.step_id,
                    request.prompt,
                    json.dumps(request.schema, ensure_ascii=False, sort_keys=True),
                    request.status,
                    _to_text(request.created_at),
                    None,
                ),
            )
        return request

    def list_pending(self, run_id: Optional[str] = None) -> List[HumanInputRequestRecord]:
        """列出待响应的人工输入请求。

        参数:
            run_id: 可选运行标识符；提供时只查询该运行。

        返回:
            待响应请求列表。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        query = "SELECT * FROM human_input_requests WHERE status = 'pending'"
        params: tuple = ()
        if run_id is not None:
            query += " AND run_id = ?"
            params = (run_id,)
        query += " ORDER BY created_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_request_from_row(row) for row in rows]

    def get_request(self, request_id: str) -> HumanInputRequestRecord:
        """按标识符返回人工输入请求。

        参数:
            request_id: 人工输入请求标识符。

        返回:
            匹配的请求记录。

        异常:
            KeyError: 如果请求不存在。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM human_input_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return _request_from_row(row)

    def record_response(
        self,
        request_id: str,
        response: Dict[str, Any],
        idempotency_key: str,
    ) -> HumanInputResponseRecord:
        """记录人工输入响应。

        参数:
            request_id: 人工输入请求标识符。
            response: 用户响应载荷。
            idempotency_key: 幂等键。

        返回:
            新建或已有的响应记录。

        异常:
            KeyError: 如果请求不存在。
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 human_input_responses 表并更新请求状态。
        """

        existing = self.get_response_by_key(idempotency_key)
        if existing is not None:
            return existing
        self.get_request(request_id)
        now = _utc_now()
        record = HumanInputResponseRecord(
            response_id=str(uuid4()),
            request_id=request_id,
            response=response,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO human_input_responses(
                    response_id, request_id, response_json, idempotency_key, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.response_id,
                    record.request_id,
                    json.dumps(record.response, ensure_ascii=False, sort_keys=True),
                    record.idempotency_key,
                    _to_text(record.created_at),
                ),
            )
            connection.execute(
                "UPDATE human_input_requests SET status = 'responded', responded_at = ? WHERE request_id = ?",
                (_to_text(now), request_id),
            )
        return record

    def get_response_by_key(self, idempotency_key: str) -> Optional[HumanInputResponseRecord]:
        """按幂等键查询人工输入响应。

        参数:
            idempotency_key: 响应幂等键。

        返回:
            存在时返回响应记录，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM human_input_responses WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _response_from_row(row) if row is not None else None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开 SQLite 连接。

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
    """将人工输入时间序列化为 SQLite 文本。

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


def _request_from_row(row: sqlite3.Row) -> HumanInputRequestRecord:
    """从 SQLite 行构造人工输入请求领域记录。

    参数:
        row: human_input_requests 查询返回的 SQLite 行。

    返回:
        反序列化后的人工输入请求记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        json.JSONDecodeError: 如果 schema_json 不是合法 JSON。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return HumanInputRequestRecord(
        request_id=row["request_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        prompt=row["prompt"],
        schema=json.loads(row["schema_json"]),
        status=row["status"],
        created_at=_from_text(row["created_at"]),
        responded_at=_from_text(row["responded_at"]) if row["responded_at"] else None,
    )


def _response_from_row(row: sqlite3.Row) -> HumanInputResponseRecord:
    """从 SQLite 行构造人工输入响应领域记录。

    参数:
        row: human_input_responses 查询返回的 SQLite 行。

    返回:
        反序列化后的人工输入响应记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        json.JSONDecodeError: 如果 response_json 不是合法 JSON。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return HumanInputResponseRecord(
        response_id=row["response_id"],
        request_id=row["request_id"],
        response=json.loads(row["response_json"]),
        idempotency_key=row["idempotency_key"],
        created_at=_from_text(row["created_at"]),
    )
