"""Durable Run State 相关 SQLite schema 初始化。"""

from contextlib import closing
from pathlib import Path
import sqlite3


def ensure_durable_schema(database_path: Path) -> None:
    """确保 Durable Run State 相关表存在。

    参数:
        database_path: 需要初始化 schema 的 SQLite 数据库路径。

    返回:
        无。

    异常:
        OSError: 如果数据库目录无法创建。
        sqlite3.Error: 如果 schema 初始化失败。

    副作用:
        创建数据库目录，并在 SQLite 中创建 Durable Run State 相关表。
    """

    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.executescript(
                """
            CREATE TABLE IF NOT EXISTS durable_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                wait_reason TEXT,
                active_step_id TEXT,
                active_wait_id TEXT,
                last_checkpoint_id TEXT,
                interruption_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resume_commands (
                command_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                applied_at TEXT
            );

            CREATE TABLE IF NOT EXISTS approval_requests (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT,
                tool_call_id TEXT,
                tool_name TEXT NOT NULL,
                permission TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS approval_decisions (
                decision_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                decision TEXT NOT NULL,
                reason TEXT,
                decided_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS human_input_requests (
                request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT,
                prompt TEXT NOT NULL,
                schema_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                responded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS human_input_responses (
                response_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                response_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tool_calls (
                tool_call_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                permission TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tool_executions (
                execution_id TEXT PRIMARY KEY,
                tool_call_id TEXT NOT NULL,
                status TEXT NOT NULL,
                effect_status TEXT NOT NULL,
                artifact_id TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                step_id TEXT,
                kind TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
                """
            )
