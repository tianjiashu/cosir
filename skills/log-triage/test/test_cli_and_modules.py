#!/usr/bin/env python3
"""log-triage CLI 与其查询模块的独立单元测试（第三轮对抗性复测）。

覆盖：参数校验、渲染、schema、agent_facts、side_effects、snapshots、
query_logs 的查询构造与边界。全部走内存库/临时库，不触碰真实库。

注：``# ruff: noqa: E501`` —— 建表与造数用的一行 SQL/JSON 字面量天然超长，
拆行会显著降低夹具可读性且无收益，故对本文件整体豁免行宽。
"""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import query_app_db as qad  # noqa: E402
import query_logs as ql  # noqa: E402
from appdb_agent_facts import (  # noqa: E402
    list_models,
    list_providers,
    recent_commands,
    recent_runs,
    recent_tasks,
    recent_workspaces,
)
from appdb_schema import database_overview, table_detail  # noqa: E402
from appdb_side_effects import (  # noqa: E402
    list_attachment_assets,
    list_delegations,
    list_file_snapshots,
    list_terminal_sessions,
)
from appdb_snapshots import run_snapshot, task_snapshot  # noqa: E402


def _schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, workspace_id INTEGER, title TEXT,
            task_type TEXT, parent_task_id INTEGER, parent_run_id INTEGER,
            current_run_id INTEGER, delegation_id INTEGER, context_usage_used INTEGER,
            context_window_total INTEGER, created_at TEXT, updated_at TEXT, extra TEXT);
        CREATE TABLE conversation_runs (id INTEGER PRIMARY KEY, task_id INTEGER,
            status TEXT, agent_id TEXT, provider_id INTEGER, model_name TEXT,
            reasoning_effort TEXT, end_reason TEXT, input_text TEXT, final_output TEXT,
            usage_json TEXT, error_json TEXT, created_at TEXT, updated_at TEXT,
            checkpoint_thread_id TEXT);
        CREATE TABLE conversation_commands (id INTEGER PRIMARY KEY, task_id INTEGER,
            command_id TEXT, command_type TEXT, payload_hash TEXT, run_id INTEGER,
            error_code TEXT, created_at TEXT);
        CREATE TABLE conversation_task_contexts (id INTEGER PRIMARY KEY, task_id INTEGER,
            run_id INTEGER, tool_call_id TEXT, message_json TEXT NOT NULL,
            transport_metadata_json TEXT NOT NULL, include_in_context BOOLEAN,
            sequence INTEGER, created_at TEXT, updated_at TEXT, is_streaming BOOLEAN);
        CREATE TABLE file_snapshots (id INTEGER PRIMARY KEY, task_id INTEGER, run_id INTEGER,
            tool_call_id TEXT, tool_name TEXT, path TEXT, action TEXT, seq INTEGER,
            additions INTEGER, deletions INTEGER, stable BOOLEAN, status TEXT,
            reverted_at TEXT, op_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE delegations (id INTEGER PRIMARY KEY, task_id INTEGER, parent_run_id INTEGER,
            child_run_id INTEGER, child_task_id INTEGER, parent_agent_id TEXT,
            child_agent_id TEXT, status TEXT, prompt TEXT, summary TEXT, error TEXT,
            effective_tools TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE terminal_sessions (id INTEGER PRIMARY KEY, session_id TEXT, task_id INTEGER,
            workspace_id INTEGER, created_by_run_id INTEGER, initial_cwd TEXT, shell_kind TEXT,
            shell_executable TEXT, worker_instance_id TEXT, worker_pid INTEGER, status TEXT,
            end_reason TEXT, exit_code INTEGER, cols INTEGER, rows INTEGER,
            last_activity_at TEXT, ended_at TEXT, created_at TEXT);
        CREATE TABLE attachment_assets (id INTEGER PRIMARY KEY, task_id INTEGER, asset_id TEXT,
            kind TEXT, content_sha256 TEXT, idempotency_key TEXT, name TEXT, content_type TEXT,
            byte_size INTEGER, width INTEGER, height INTEGER, storage_state TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE providers (id INTEGER PRIMARY KEY, name TEXT, type TEXT, base_url TEXT,
            api_key TEXT, enabled BOOLEAN, sort_order INTEGER);
        CREATE TABLE models (id INTEGER PRIMARY KEY, provider_id INTEGER, model_name TEXT,
            display_name TEXT, max_context_window INTEGER, supports_thinking BOOLEAN,
            supports_image BOOLEAN, supports_video BOOLEAN, enabled BOOLEAN, sort_order INTEGER);
        """
    )
    con.execute("INSERT INTO workspaces VALUES (1,'ws1','/tmp/ws1','t','t')")
    con.execute("INSERT INTO tasks VALUES (1,1,'t1','chat',NULL,NULL,1,NULL,0,100,'t','t',NULL)")
    con.execute(
        "INSERT INTO conversation_runs VALUES (1,1,'completed','a',1,'m',NULL,'done','i','o','{}','{}','t','t',NULL)"
    )
    con.commit()


@pytest.fixture()
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _schema(connection)
    yield connection
    connection.close()


# --------------------------------------------------------------------------- #
# query_app_db：纯函数与参数校验
# --------------------------------------------------------------------------- #


class TestNormalizeLimit:
    # 目的：limit 边界 1 与上限合法，0/负/超限非法。潜在缺陷：off-by-one。
    @pytest.mark.parametrize("value", [1, 50, qad._MAX_LIMIT])
    def test_valid(self, value: int) -> None:
        assert qad.normalize_limit(value) == value

    @pytest.mark.parametrize("value", [0, -1, qad._MAX_LIMIT + 1])
    def test_invalid(self, value: int) -> None:
        with pytest.raises(ValueError):
            qad.normalize_limit(value)


class TestCompactText:
    # 目的：None 渲染为空串；长文本按 max_chars 截断加省略号。潜在缺陷：截断越界。
    def test_none_is_empty(self) -> None:
        assert qad.compact_text(None) == ""

    def test_truncation(self) -> None:
        assert qad.compact_text("abcdefghij", max_chars=6) == "abc..."

    def test_collapses_whitespace(self) -> None:
        assert qad.compact_text("a  b\n c") == "a b c"

    def test_dict_serialized(self) -> None:
        assert qad.compact_text({"a": 1}) == '{"a":1}'


class TestRender:
    # 目的：空列表渲染 (no rows)；dict 渲染分组；标量直接渲染。潜在缺陷：空值分组渲染异常。
    def test_empty_list(self) -> None:
        assert qad.render([]) == "(no rows)"

    def test_list_of_mappings_skips_empty(self) -> None:
        out = qad.render([{"a": 1, "b": None, "c": ""}])
        assert out == "a=1"

    def test_dict_with_empty_list(self) -> None:
        assert "[k]\n(none)" in qad.render({"k": []})

    def test_scalar(self) -> None:
        assert qad.render(42) == "42"


class TestEmit:
    # 目的：非法格式抛 ValueError。潜在缺陷：静默降级。
    def test_bad_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(ValueError):
            qad.emit([], output_format="xml")


class TestSaveOutput:
    # 目的：已存在且未 force 抛 FileExistsError；force 覆盖；json 格式内容合法。潜在缺陷：静默覆盖。
    def test_exists_without_force(self, tmp_path: Path) -> None:
        target = tmp_path / "o.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(FileExistsError):
            qad.save_output(target, [{"a": 1}], output_format="text", force=False)

    def test_force_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "o.json"
        target.write_text("old", encoding="utf-8")
        qad.save_output(target, [{"a": 1}], output_format="json", force=True)
        assert json.loads(target.read_text(encoding="utf-8")) == [{"a": 1}]

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "o.txt"
        qad.save_output(target, [{"a": 1}], output_format="text", force=False)
        assert target.exists()


class TestDispatch:
    # 目的：未知命令抛 ValueError。潜在缺陷：静默返回 None。
    def test_unknown_command(self, con: sqlite3.Connection) -> None:
        args = qad.build_parser().parse_args(["tasks"])
        args.command = "nope"
        with pytest.raises(ValueError):
            qad.dispatch(con, args)


# --------------------------------------------------------------------------- #
# appdb_schema
# --------------------------------------------------------------------------- #


class TestSchema:
    # 目的：overview 列出所有表且行数正确；table_detail 已知表返回列；未知表抛带清单的 ValueError。
    def test_overview(self, con: sqlite3.Connection) -> None:
        out = database_overview(con, include_columns=False)
        names = {t["table"] for t in out["tables"]}
        assert {"tasks", "workspaces", "providers"} <= names
        assert all("columns" not in t for t in out["tables"])

    def test_detail_unknown_table(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError) as excinfo:
            table_detail(con, "ghost")
        assert "table not found: ghost" in str(excinfo.value)
        assert "tables in this database" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# appdb_agent_facts
# --------------------------------------------------------------------------- #


class TestAgentFacts:
    # 目的：providers 不得返回明文 api_key 列，只返回 has_api_key 布尔。潜在缺陷：secret 泄漏。
    def test_providers_no_plaintext_key(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO providers VALUES (1,'p','api','http://x','SECRET-123',1,0)")
        con.commit()
        rows = list_providers(con, limit=10)
        assert "api_key" not in rows[0]
        assert rows[0]["has_api_key"] == 1
        assert "SECRET-123" not in json.dumps(rows)

    # 目的：has_api_key 对 NULL/空串为 0，非空为 1。潜在缺陷：空串被当有效 key。
    def test_has_api_key_null_and_empty(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO providers VALUES (1,'a','api','u',NULL,1,0)")
        con.execute("INSERT INTO providers VALUES (2,'b','api','u','',1,1)")
        con.execute("INSERT INTO providers VALUES (3,'c','api','u','k',1,2)")
        con.commit()
        rows = {r["id"]: r["has_api_key"] for r in list_providers(con, limit=10)}
        assert rows == {1: 0, 2: 0, 3: 1}

    # 目的：recent_runs 非法 status 抛 ValueError。潜在缺陷：非法状态静默返回空。
    def test_recent_runs_invalid_status(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            recent_runs(con, limit=10, status="bogus")

    # 目的：recent_tasks contains 对 LIKE 通配符转义，% 不扩大匹配面。潜在缺陷：未转义导致通配。
    def test_tasks_contains_escapes_wildcard(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO tasks VALUES (2,1,'abc','chat',NULL,NULL,NULL,NULL,0,1,'t','t',NULL)"
        )
        con.execute(
            "INSERT INTO tasks VALUES (3,1,'a%c','chat',NULL,NULL,NULL,NULL,0,1,'t','t',NULL)"
        )
        con.commit()
        rows = recent_tasks(con, limit=10, contains="%")
        titles = [r["title"] for r in rows]
        assert titles == ["a%c"]

    # 目的：workspaces 按 id 降序、limit 生效。潜在缺陷：排序错。
    def test_workspaces_order(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO workspaces VALUES (2,'ws2','/x','t','t')")
        con.commit()
        assert [r["id"] for r in recent_workspaces(con, limit=10)] == [2, 1]

    # 目的：models provider_id 过滤生效。潜在缺陷：过滤被忽略。
    def test_models_provider_filter(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO models VALUES (1,1,'m1','M1',100,0,0,0,1,0)")
        con.execute("INSERT INTO models VALUES (2,2,'m2','M2',100,0,0,0,1,1)")
        con.commit()
        assert [r["id"] for r in list_models(con, limit=10, provider_id=2)] == [2]
        assert len(list_models(con, limit=10)) == 2

    # 目的：commands 可选 task/run 过滤。潜在缺陷：过滤组合错。
    def test_commands_filters(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO conversation_commands VALUES (1,1,'c1','t','h',1,NULL,'t')")
        con.execute("INSERT INTO conversation_commands VALUES (2,1,'c2','t','h',2,NULL,'t')")
        con.commit()
        assert [r["id"] for r in recent_commands(con, limit=10, run_id=2)] == [2]
        assert len(recent_commands(con, limit=10, task_id=1)) == 2


# --------------------------------------------------------------------------- #
# appdb_side_effects
# --------------------------------------------------------------------------- #


class TestSideEffects:
    # 目的：file_snapshots 不返回 op_json 原文，只返回长度提示 op_chars。潜在缺陷：大载荷泄漏。
    def test_file_snapshots_hides_op_json(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO file_snapshots VALUES (1,1,1,'tc','w','/f','mod',1,1,1,1,'ok',NULL,'PAYLOAD',  't','t')"
        )
        con.commit()
        rows = list_file_snapshots(con, limit=10)
        assert "op_json" not in rows[0]
        assert rows[0]["op_chars"] == len("PAYLOAD")

    # 目的：terminal_sessions 非法 status 抛 ValueError。潜在缺陷：非法状态静默返回空列表。
    def test_terminal_invalid_status(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            list_terminal_sessions(con, limit=10, status="zombie")

    # 目的：delegations task_id 匹配发起端或子端。潜在缺陷：只匹配一侧。
    def test_delegations_match_either_side(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO delegations VALUES (1,1,NULL,NULL,5,'a','b','ok','p','s',NULL,NULL,'t','t')"
        )
        con.execute(
            "INSERT INTO delegations VALUES (2,9,NULL,NULL,5,'a','b','ok','p','s',NULL,NULL,'t','t')"
        )
        con.commit()
        ids = sorted(r["id"] for r in list_delegations(con, limit=10, task_id=5))
        assert ids == [1, 2]

    # 目的：attachments task_id 过滤生效。潜在缺陷：过滤被忽略。
    def test_attachments_filter(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO attachment_assets VALUES (1,1,'a','image','sha','ik','n','png',1,1,1,'stored','t','t')"
        )
        con.commit()
        assert len(list_attachment_assets(con, limit=10, task_id=1)) == 1
        assert list_attachment_assets(con, limit=10, task_id=2) == []


# --------------------------------------------------------------------------- #
# appdb_snapshots
# --------------------------------------------------------------------------- #


class TestSnapshots:
    # 目的：task_snapshot 不存在的 task 抛 ValueError。潜在缺陷：返回 None 泄漏到渲染层。
    def test_task_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            task_snapshot(con, task_id=999, limit=10)

    # 目的：task_snapshot 聚合结构含全部关键块。潜在缺陷：缺失 key 导致渲染崩溃。
    def test_task_snapshot_shape(self, con: sqlite3.Connection) -> None:
        snap = task_snapshot(con, task_id=1, limit=10)
        assert set(snap) == {
            "task",
            "workspace",
            "runs",
            "commands",
            "child_tasks",
            "delegations",
            "file_changes",
            "context",
        }

    # 目的：run_snapshot 不存在的 run 抛 ValueError。潜在缺陷：None 泄漏。
    def test_run_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            run_snapshot(con, run_id=999, limit=10)

    # 目的：run_snapshot 的 task_id 从 run 行取，task_id=0 不崩溃。潜在缺陷：孤儿 run 触发异常。
    def test_run_snapshot_orphan_run(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO conversation_runs VALUES (9,777,'completed','a',1,'m',NULL,'d','i','o','{}','{}','t','t',NULL)"
        )
        con.commit()
        snap = run_snapshot(con, run_id=9, limit=10)
        assert snap["run"]["id"] == 9
        assert snap["task"] is None


# --------------------------------------------------------------------------- #
# query_logs：查询构造与参数校验
# --------------------------------------------------------------------------- #


class TestQueryLogs:
    # 目的：级别归一，WARN 映射 WARNING，非法抛错。潜在缺陷：WARN 漏映射。
    def test_normalize_level(self) -> None:
        assert ql.normalize_level("warn") == "WARNING"
        assert ql.normalize_level(" info ") == "INFO"
        assert ql.normalize_level("") == ""
        with pytest.raises(ValueError):
            ql.normalize_level("verbose")

    # 目的：多个最低级别开关互斥。潜在缺陷：多开关静默取一个。
    def test_min_level_conflict(self) -> None:
        with pytest.raises(ValueError):
            ql.normalize_min_level("INFO", errors_only=True, warnings_up=False)
        assert ql.normalize_min_level("", errors_only=True, warnings_up=False) == "ERROR"
        assert ql.normalize_min_level("", errors_only=False, warnings_up=True) == "WARNING"

    # 目的：时间窗解析单位与数值边界。潜在缺陷：0 或负值被接受。
    @pytest.mark.parametrize("value", ["0s", "1x", "s", "abc"])
    def test_duration_invalid(self, value: str) -> None:
        with pytest.raises(ValueError):
            ql.normalize_duration(value)

    def test_duration_valid(self) -> None:
        import datetime

        assert ql.normalize_duration("90s") == datetime.timedelta(seconds=90)
        assert ql.normalize_duration("2m") == datetime.timedelta(minutes=2)
        assert ql.normalize_duration("1h") == datetime.timedelta(hours=1)

    # 目的：normalize_time 接受 Z 与偏移，统一 UTC。潜在缺陷：时区处理错误。
    def test_normalize_time(self) -> None:
        assert ql.normalize_time("2026-09-13T10:00:00Z") == "2026-09-13T10:00:00.000Z"
        assert ql.normalize_time("2026-09-13T18:00:00+08:00") == "2026-09-13T10:00:00.000Z"
        assert ql.normalize_time("") == ""
        with pytest.raises(ValueError):
            ql.normalize_time("not-a-time")

    # 目的：build_query 的 min_level 展开为级别 IN 且参数占位符数量匹配。潜在缺陷：SQL 与参数错位。
    def test_build_query_min_level(self) -> None:
        sql, params = ql.build_query(min_level="WARNING", limit=10)
        assert "level IN (?, ?, ?)" in sql
        assert "WARNING" in params and "CRITICAL" in params
        assert params[-1] == 10

    # 目的：build_query 非法 order/min_level 抛错。潜在缺陷：非法值进入 SQL。
    def test_build_query_invalid(self) -> None:
        with pytest.raises(ValueError):
            ql.build_query(order="sideways")
        with pytest.raises(ValueError):
            ql.build_query(min_level="TRACE")

    # 目的：limit 边界校验。潜在缺陷：0/超限被接受。
    @pytest.mark.parametrize("value", [0, -5, ql._MAX_LIMIT + 1])
    def test_limit_invalid(self, value: int) -> None:
        with pytest.raises(ValueError):
            ql.normalize_limit(value)

    # 目的：--around 与 --since/--until 互斥。潜在缺陷：组合被忽略。
    def test_around_conflict(self) -> None:
        parser = ql.build_parser()
        args = parser.parse_args(
            ["recent", "--around", "2026-01-01T00:00:00Z", "--since", "2026-01-01T00:00:00Z"]
        )
        with pytest.raises(ValueError):
            ql.resolve_time_range(args)

    # 目的：trace 子命令空 trace_id 抛错。潜在缺陷：空 id 全表返回。
    def test_trace_blank_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["trace", "   "])
        assert rc == 1

    # 目的：escape_like 与 contains_pattern 一致转义。潜在缺陷：转义遗漏。
    def test_escape_like(self) -> None:
        assert ql.escape_like("100%_\\") == "100\\%\\_\\\\"

    # 目的：row_to_entry 处理 truncated 为 TEXT 'false' 时不误判为 True。潜在缺陷：bool('false') 陷阱。
    def test_row_to_entry_truncated_text_false(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE log_entries (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, logger TEXT,"
            " trace_id TEXT, caller TEXT, event TEXT, msg TEXT, data_json TEXT, error_json TEXT, truncated TEXT)"
        )
        con.execute(
            "INSERT INTO log_entries VALUES (1,'ts','INFO','l','','c','e','m','{}',NULL,'false')"
        )
        row = con.execute("SELECT * FROM log_entries").fetchone()
        entry = ql.row_to_entry(row)
        assert entry["truncated"] is False
        con.close()
