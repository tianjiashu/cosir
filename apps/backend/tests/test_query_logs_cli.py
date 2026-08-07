"""``scripts/query_logs.py`` 日志查询 CLI 的单元测试。

覆盖参数归一化、SQL 构造、只读连接、行转换、文本渲染与 main 入口的成功/失败路径。
脚本不在 ``pythonpath`` 上（位于仓库根 ``scripts/``），故按文件路径动态加载模块。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "query_logs.py"


def _load_cli() -> ModuleType:
    """按文件路径动态加载 CLI 脚本模块。

    参数:
        无。

    返回:
        已加载的 ``query_logs`` 模块。

    异常:
        ImportError: 如果脚本文件不存在或无法加载。

    副作用:
        向 ``sys.modules`` 注册模块（键为 ``query_logs_cli``）。
    """

    spec = importlib.util.spec_from_file_location("query_logs_cli", _SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load CLI script: {_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


@pytest.fixture
def log_db(tmp_path: Path) -> Path:
    """创建带样例数据的临时日志库，schema 与 ``log_entries`` 表一致。"""

    db_path = tmp_path / "logs.sqlite3"
    connection = sqlite3.connect(db_path)
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
            "2026-08-01T00:00:00.000Z",
            "INFO",
            "coding_agent.backend",
            "trace-a",
            "api:handler:10",
            "http_request_started",
            "请求开始",
            '{"method": "GET"}',
            None,
            0,
        ),
        (
            "2026-08-01T00:00:01.000Z",
            "ERROR",
            "coding_agent.backend",
            "trace-a",
            "api:handler:20",
            "http_request_failed",
            "请求失败",
            '{"status": 500}',
            '{"type": "ValueError"}',
            1,
        ),
        (
            "2026-08-02T00:00:00.000Z",
            "INFO",
            "coding_agent.backend",
            "trace-b",
            "",
            "tool_call_finished",
            "工具完成",
            "{}",
            None,
            0,
        ),
    ]
    connection.executemany(
        "INSERT INTO log_entries "
        "(ts, level, logger, trace_id, caller, event, msg, data_json, error_json, truncated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()
    return db_path


class TestNormalizeLevel:
    """``normalize_level`` 的级别归一化与校验。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("", ""), ("  ", ""), ("info", "INFO"), ("warn", "WARNING"), (" Error ", "ERROR")],
    )
    def test_normalizes_supported_levels(self, raw: str, expected: str) -> None:
        assert cli.normalize_level(raw) == expected

    def test_rejects_unknown_level(self) -> None:
        with pytest.raises(ValueError, match="level must be one of"):
            cli.normalize_level("verbose")


class TestNormalizeTime:
    """``normalize_time`` 的 RFC3339 归一化。"""

    def test_blank_returns_empty(self) -> None:
        assert cli.normalize_time("  ") == ""

    def test_zulu_suffix_kept(self) -> None:
        assert cli.normalize_time("2026-08-01T00:00:00Z") == "2026-08-01T00:00:00.000Z"

    def test_naive_time_treated_as_utc(self) -> None:
        assert cli.normalize_time("2026-08-01T00:00:00") == "2026-08-01T00:00:00.000Z"

    def test_offset_converted_to_utc(self) -> None:
        assert cli.normalize_time("2026-08-01T08:00:00+08:00") == "2026-08-01T00:00:00.000Z"

    def test_rejects_invalid_text(self) -> None:
        with pytest.raises(ValueError):
            cli.normalize_time("not-a-time")


class TestNormalizeLimit:
    """``normalize_limit`` 的区间校验。"""

    def test_accepts_valid_limit(self) -> None:
        assert cli.normalize_limit(50) == 50

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            cli.normalize_limit(0)

    def test_rejects_over_max(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to"):
            cli.normalize_limit(cli._MAX_LIMIT + 1)


class TestBuildQuery:
    """``build_query`` 的 SQL 与参数构造。"""

    def test_no_filters_only_limit(self) -> None:
        sql, params = cli.build_query(limit=10)
        assert "WHERE" not in sql
        assert sql.endswith("ORDER BY ts ASC, id ASC LIMIT ?")
        assert params == [10]

    def test_all_filters_use_placeholders(self) -> None:
        sql, params = cli.build_query(
            trace_id="t1",
            level="ERROR",
            event="e1",
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-08-02T00:00:00.000Z",
            limit=5,
            order="desc",
        )
        assert "trace_id = ?" in sql
        assert "level = ?" in sql
        assert "event = ?" in sql
        assert "ts >= ?" in sql
        assert "ts <= ?" in sql
        assert "ORDER BY ts DESC, id DESC" in sql
        assert params == [
            "t1",
            "ERROR",
            "e1",
            "2026-08-01T00:00:00.000Z",
            "2026-08-02T00:00:00.000Z",
            5,
        ]

    def test_rejects_bad_order(self) -> None:
        with pytest.raises(ValueError, match="order must be asc or desc"):
            cli.build_query(order="sideways")


class TestOpenReadonly:
    """``open_readonly`` 的只读语义与缺失文件处理。"""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="log database not found"):
            cli.open_readonly(tmp_path / "absent.sqlite3")

    def test_write_is_rejected(self, log_db: Path) -> None:
        connection = cli.open_readonly(log_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("DELETE FROM log_entries")
        finally:
            connection.close()


class TestRunQuery:
    """``run_query`` 的端到端查询与过滤。"""

    def test_returns_all_ordered_desc(self, log_db: Path) -> None:
        sql, params = cli.build_query(limit=10, order="desc")
        entries = cli.run_query(log_db, sql, params)
        assert [entry["ts"] for entry in entries] == [
            "2026-08-02T00:00:00.000Z",
            "2026-08-01T00:00:01.000Z",
            "2026-08-01T00:00:00.000Z",
        ]

    def test_filters_by_trace(self, log_db: Path) -> None:
        sql, params = cli.build_query(trace_id="trace-a", limit=10)
        entries = cli.run_query(log_db, sql, params)
        assert len(entries) == 2
        assert {entry["trace_id"] for entry in entries} == {"trace-a"}

    def test_filters_by_level_and_time_range(self, log_db: Path) -> None:
        sql, params = cli.build_query(
            level="ERROR",
            start_time="2026-08-01T00:00:00.000Z",
            end_time="2026-08-01T23:59:59.000Z",
            limit=10,
        )
        entries = cli.run_query(log_db, sql, params)
        assert len(entries) == 1
        assert entries[0]["event"] == "http_request_failed"

    def test_limit_applied(self, log_db: Path) -> None:
        sql, params = cli.build_query(limit=1)
        assert len(cli.run_query(log_db, sql, params)) == 1

    def test_entry_fields_decoded(self, log_db: Path) -> None:
        sql, params = cli.build_query(level="ERROR", limit=1)
        entry = cli.run_query(log_db, sql, params)[0]
        assert entry["data"] == {"status": 500}
        assert entry["error"] == {"type": "ValueError"}
        assert entry["truncated"] is True

    def test_nullable_columns_fallback_to_blank(self, log_db: Path) -> None:
        sql, params = cli.build_query(trace_id="trace-b", limit=1)
        entry = cli.run_query(log_db, sql, params)[0]
        assert entry["caller"] == ""
        assert entry["data"] == {}
        assert entry["error"] is None


class TestRowToEntry:
    """``row_to_entry`` 对畸形 JSON 的宽容回退。"""

    def _row(self, tmp_path: Path, data_json: str, error_json: str | None) -> sqlite3.Row:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE log_entries (ts TEXT, level TEXT, logger TEXT, trace_id TEXT, "
            "caller TEXT, event TEXT, msg TEXT, data_json TEXT, error_json TEXT, truncated INT)"
        )
        connection.execute(
            "INSERT INTO log_entries VALUES ('t','INFO','lg',NULL,NULL,'e','m',?,?,0)",
            (data_json, error_json),
        )
        row = connection.execute("SELECT * FROM log_entries").fetchone()
        connection.close()
        return row

    def test_malformed_data_json_falls_back(self, tmp_path: Path) -> None:
        entry = cli.row_to_entry(self._row(tmp_path, "{not json", None))
        assert entry["data"] == {}

    def test_non_dict_data_json_falls_back(self, tmp_path: Path) -> None:
        entry = cli.row_to_entry(self._row(tmp_path, "[1, 2]", None))
        assert entry["data"] == {}

    def test_non_dict_error_json_falls_back(self, tmp_path: Path) -> None:
        entry = cli.row_to_entry(self._row(tmp_path, "{}", '"boom"'))
        assert entry["error"] is None

    def test_null_trace_and_caller_become_blank(self, tmp_path: Path) -> None:
        entry = cli.row_to_entry(self._row(tmp_path, "{}", None))
        assert entry["trace_id"] == ""
        assert entry["caller"] == ""


class TestRenderText:
    """``render_text`` 的文本渲染格式。"""

    def test_empty_entries_render_blank(self) -> None:
        assert cli.render_text([]) == ""

    def test_renders_core_fields(self) -> None:
        entry = {
            "ts": "2026-08-01T00:00:00.000Z",
            "level": "INFO",
            "logger": "lg",
            "trace_id": "tr",
            "caller": "mod:fn:1",
            "event": "ev",
            "msg": "消息",
            "data": {"a": 1},
            "error": None,
            "truncated": False,
        }
        line = cli.render_text([entry])
        assert line.startswith("2026-08-01T00:00:00.000Z INFO lg event=ev")
        assert "trace_id=tr" in line
        assert "caller=mod:fn:1" in line
        assert 'msg="消息"' in line
        assert "data.a=1" in line
        assert "{" not in line

    def test_omits_blank_trace_and_caller(self) -> None:
        entry = {
            "ts": "t",
            "level": "INFO",
            "logger": "lg",
            "trace_id": "",
            "caller": "",
            "event": "ev",
            "msg": "m",
            "data": {},
            "error": None,
            "truncated": False,
        }
        line = cli.render_text([entry])
        assert "trace_id=" not in line
        assert "caller=" not in line

    def test_flattens_nested_containers(self) -> None:
        entry = {
            "ts": "t",
            "level": "INFO",
            "logger": "lg",
            "trace_id": "",
            "caller": "",
            "event": "ev",
            "msg": "m",
            "data": {"outer": {"inner": 1}, "items": [7, 8]},
            "error": {"type": "ValueError"},
            "truncated": False,
        }
        line = cli.render_text([entry])
        assert "data.outer.inner=1" in line
        assert "data.items[0]=7" in line
        assert "data.items[1]=8" in line
        assert "error.type=ValueError" in line


class TestResolveDbPath:
    """``resolve_db_path`` 与 ``default_db_path`` 的路径推导。"""

    def test_blank_uses_default(self) -> None:
        assert cli.resolve_db_path("  ") == cli.default_db_path()

    def test_explicit_path_used(self, tmp_path: Path) -> None:
        target = tmp_path / "custom.sqlite3"
        assert cli.resolve_db_path(str(target)) == target

    def test_default_points_to_repository_storage(self) -> None:
        default = cli.default_db_path()
        assert default.name == "logs.sqlite3"
        assert default.parent.name == "storage"


class TestMain:
    """``main`` 入口的成功与失败路径。"""

    def test_recent_text_output(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["recent", "--db", str(log_db), "--limit", "2"])
        out = capsys.readouterr().out
        assert code == 0
        assert out.splitlines()[0].startswith("2026-08-02T00:00:00.000Z")
        assert len(out.strip().splitlines()) == 2

    def test_recent_json_output(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["recent", "--db", str(log_db), "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert len(payload) == 3
        assert payload[0]["ts"] == "2026-08-02T00:00:00.000Z"

    def test_recent_filters_by_event(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(
            ["recent", "--db", str(log_db), "--event", "tool_call_finished", "--format", "json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert [entry["event"] for entry in payload] == ["tool_call_finished"]

    def test_trace_output_ordered_asc(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["trace", "trace-a", "--db", str(log_db), "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert [entry["ts"] for entry in payload] == [
            "2026-08-01T00:00:00.000Z",
            "2026-08-01T00:00:01.000Z",
        ]

    def test_trace_blank_id_fails(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["trace", "   ", "--db", str(log_db)])
        assert code == 1
        assert "trace_id must not be blank" in capsys.readouterr().err

    def test_invalid_level_fails(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["recent", "--db", str(log_db), "--level", "nope"])
        assert code == 1
        assert "error:" in capsys.readouterr().err

    def test_invalid_limit_fails(
        self, log_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["recent", "--db", str(log_db), "--limit", "0"])
        assert code == 1
        assert "greater than zero" in capsys.readouterr().err

    def test_missing_db_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = cli.main(["recent", "--db", str(tmp_path / "absent.sqlite3")])
        assert code == 1
        assert "log database not found" in capsys.readouterr().err

    def test_sqlite_error_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        broken = tmp_path / "broken.sqlite3"
        broken.write_text("not a database", encoding="utf-8")
        code = cli.main(["recent", "--db", str(broken)])
        assert code == 1
        assert "error:" in capsys.readouterr().err

    def test_no_subcommand_exits_with_usage_error(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main([])
        assert excinfo.value.code == 2
