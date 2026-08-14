from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any


def _load_script_module() -> Any:
    """加载 query_logs 脚本模块。

    参数:
        无。
    返回:
        已加载的脚本模块对象。
    异常:
        AssertionError: 如果脚本 spec 无法创建。
    副作用:
        从 scripts/query_logs.py 执行模块加载。
    """

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "query_logs.py"
    spec = importlib.util.spec_from_file_location("query_logs", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_log_db(tmp_path: Path) -> Path:
    """创建包含测试日志的临时 SQLite 数据库。

    参数:
        tmp_path: pytest 提供的临时目录。
    返回:
        临时日志数据库路径。
    异常:
        sqlite3.Error: 如果建表或插入数据失败。
    副作用:
        在临时目录写入 logs.sqlite3。
    """

    db_path = tmp_path / "logs.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                logger TEXT NOT NULL,
                trace_id TEXT,
                caller TEXT,
                event TEXT NOT NULL,
                msg TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT,
                truncated INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        rows = [
            (
                "2026-08-13T01:00:00.000Z",
                "INFO",
                "coding_agent.backend",
                "trace-a",
                "app.service.task:TaskService.create:10",
                "task_created",
                "任务已创建",
                {"task_id": "t-1"},
                None,
            ),
            (
                "2026-08-13T01:01:00.000Z",
                "WARNING",
                "coding_agent.backend",
                "trace-a",
                "app.tools.tool_execute:ToolExecutor.run:20",
                "tool_call_timeout",
                "工具调用超时",
                {"tool": "read_file"},
                None,
            ),
            (
                "2026-08-13T01:02:00.000Z",
                "ERROR",
                "coding_agent.backend",
                "trace-b",
                "app.storage.task_crud:TaskCrud.save:30",
                "db_write_failed",
                "数据库写入失败",
                {"operation": "save"},
                {"type": "OperationalError", "message": "database is locked"},
            ),
            (
                "2026-08-13T01:04:00.000Z",
                "INFO",
                "coding_agent.backend",
                "trace-c",
                "app.core.runtime.runner:AgentRuntime.run:40",
                "turn_finished",
                "运行完成",
                {"status": "success"},
                None,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO log_entries (
                ts, level, logger, trace_id, caller, event, msg, data_json, error_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ts,
                    level,
                    logger,
                    trace_id,
                    caller,
                    event,
                    msg,
                    json.dumps(data, ensure_ascii=False),
                    json.dumps(error, ensure_ascii=False) if error else None,
                )
                for ts, level, logger, trace_id, caller, event, msg, data, error in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def test_recent_filters_by_contains_event_prefix_and_caller(
    tmp_path: Path,
    capsys: Any,
) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)

    exit_code = module.main(
        [
            "recent",
            "--db",
            str(db_path),
            "--contains",
            "read_file",
            "--event-prefix",
            "tool_",
            "--caller-contains",
            "tool_execute",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert [entry["event"] for entry in output] == ["tool_call_timeout"]


def test_like_filters_treat_percent_and_underscore_as_literal(tmp_path: Path) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)

    sql, params = module.build_query(
        contains="%",
        event_prefix="tool_",
        caller_contains="_execute",
        limit=10,
        order="asc",
    )
    entries = module.run_query(db_path, sql, params)

    assert entries == []


def test_cli_errors_only_outputs_only_errors(tmp_path: Path, capsys: Any) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)

    exit_code = module.main(
        [
            "recent",
            "--db",
            str(db_path),
            "--errors-only",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert [entry["event"] for entry in output] == ["db_write_failed"]


def test_cli_save_refuses_existing_file_without_force(tmp_path: Path, capsys: Any) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)
    output_path = tmp_path / "existing.json"
    output_path.write_text("old", encoding="utf-8")

    exit_code = module.main(
        [
            "recent",
            "--db",
            str(db_path),
            "--limit",
            "1",
            "--format",
            "json",
            "--save",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "already exists" in captured.err
    assert output_path.read_text(encoding="utf-8") == "old"


def test_cli_save_force_overwrites_existing_file(tmp_path: Path, capsys: Any) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)
    output_path = tmp_path / "existing.json"
    output_path.write_text("old", encoding="utf-8")

    exit_code = module.main(
        [
            "recent",
            "--db",
            str(db_path),
            "--limit",
            "1",
            "--format",
            "json",
            "--save",
            str(output_path),
            "--force",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)
    assert json.loads(output_path.read_text(encoding="utf-8"))


def test_build_query_supports_warning_up_and_time_window(tmp_path: Path) -> None:
    module = _load_script_module()
    db_path = _create_log_db(tmp_path)
    start_time, end_time = module.normalize_around_window("2026-08-13T01:01:00Z", "90s")

    sql, params = module.build_query(
        start_time=start_time,
        end_time=end_time,
        min_level="WARNING",
        limit=10,
        order="asc",
    )
    entries = module.run_query(db_path, sql, params)

    assert [entry["event"] for entry in entries] == ["tool_call_timeout", "db_write_failed"]


def test_save_output_writes_json_file(tmp_path: Path) -> None:
    module = _load_script_module()
    output_path = tmp_path / "exports" / "logs.json"

    written = module.write_output_file(
        output_path,
        [{"event": "db_write_failed"}],
        output_format="json",
    )

    assert written == output_path
    assert json.loads(output_path.read_text(encoding="utf-8")) == [{"event": "db_write_failed"}]
