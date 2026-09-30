#!/usr/bin/env python3
"""log-triage CLI 与其查询模块的独立单元测试（第三轮对抗性复测）。

覆盖：参数校验、渲染、schema、agent_facts（含委派子任务视图）、snapshots、
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
    list_child_tasks,
    list_model_configs,
    recent_commands,
    recent_runs,
    recent_tasks,
    recent_workspaces,
)
from appdb_schema import database_overview, table_detail  # noqa: E402
from appdb_snapshots import run_snapshot, task_snapshot  # noqa: E402


def _schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE model_configs (id INTEGER PRIMARY KEY, config_name TEXT NOT NULL,
            base_url TEXT NOT NULL, api_key TEXT NOT NULL, model_name TEXT NOT NULL,
            context_window_k INTEGER NOT NULL, supports_thinking BOOLEAN NOT NULL DEFAULT 0,
            supports_reasoning_effort BOOLEAN NOT NULL DEFAULT 0,
            supports_image BOOLEAN NOT NULL DEFAULT 0,
            enabled BOOLEAN NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL,
            creation_command_id TEXT, title TEXT NOT NULL, extra TEXT,
            task_type TEXT NOT NULL, parent_task_id INTEGER, parent_run_id INTEGER,
            current_run_id INTEGER, context_window_total INTEGER,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE conversation_runs (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,
            checkpoint_thread_id TEXT, input_text TEXT NOT NULL, agent_id TEXT,
            model_config_id INTEGER, image_paths TEXT, end_reason TEXT, final_output TEXT,
            extra TEXT, usage_json TEXT, error_json TEXT, status TEXT NOT NULL,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE conversation_commands (id INTEGER PRIMARY KEY, task_id INTEGER,
            command_id TEXT, command_type TEXT, payload_hash TEXT, run_id INTEGER,
            error_code TEXT, created_at TEXT);
        CREATE TABLE conversation_task_contexts (id INTEGER PRIMARY KEY, task_id INTEGER,
            run_id INTEGER, tool_call_id TEXT, message_json TEXT NOT NULL,
            transport_metadata_json TEXT NOT NULL, include_in_context BOOLEAN,
            is_streaming BOOLEAN, sequence INTEGER, created_at TEXT, updated_at TEXT);
        """
    )
    con.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, updated_at) "
        "VALUES (1, 'ws1', '/tmp/ws1', 't', 't')"
    )
    con.execute(
        "INSERT INTO model_configs (id, config_name, base_url, api_key, model_name, "
        "context_window_k, supports_thinking, supports_reasoning_effort, supports_image, "
        "enabled, sort_order, created_at, updated_at) "
        "VALUES (1, 'deepseek', 'https://api.deepseek.com', 'sk-secret', 'deepseek-flash', "
        "1000, 1, 1, 1, 1, 0, 't', 't')"
    )
    con.execute(
        "INSERT INTO tasks (id, workspace_id, creation_command_id, title, extra, task_type, "
        "parent_task_id, parent_run_id, current_run_id, "
        "context_window_total, created_at, updated_at) "
        "VALUES (1, 1, NULL, 't1', NULL, 'user', NULL, NULL, 1, 100, 't', 't')"
    )
    con.execute(
        "INSERT INTO conversation_runs (id, task_id, checkpoint_thread_id, input_text, "
        "agent_id, model_config_id, image_paths, end_reason, final_output, extra, usage_json, "
        "error_json, status, created_at, updated_at) "
        "VALUES (1, 1, NULL, 'i', 'a', 1, NULL, 'done', 'o', NULL, '{}', '{}', 'completed', "
        "'t', 't')"
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
        assert {"tasks", "workspaces", "model_configs"} <= names
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
    # 目的：model_configs 不得返回明文 api_key 列，只返回 has_api_key 布尔。潜在缺陷：secret 泄漏。
    def test_model_configs_no_plaintext_key(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO model_configs (id, config_name, base_url, api_key, model_name, "
            "context_window_k, enabled, sort_order) "
            "VALUES (2, 'p', 'http://x', 'SECRET-123', 'm', 64, 1, 0)"
        )
        con.commit()
        rows = list_model_configs(con, limit=10)
        row = next(item for item in rows if item["id"] == 2)
        assert "api_key" not in row
        assert row["has_api_key"] == 1
        assert "SECRET-123" not in json.dumps(rows)

    # 目的：has_api_key 对空串为 0、非空为 1（api_key 列 NOT NULL）。潜在缺陷：空串被当有效 key。
    def test_has_api_key_empty_and_present(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO model_configs (id, config_name, base_url, api_key, model_name, "
            "context_window_k, enabled, sort_order) "
            "VALUES (2, 'empty', 'u', '', 'm', 64, 1, 1)"
        )
        con.execute(
            "INSERT INTO model_configs (id, config_name, base_url, api_key, model_name, "
            "context_window_k, enabled, sort_order) "
            "VALUES (3, 'filled', 'u', 'k', 'm', 64, 1, 2)"
        )
        con.commit()
        rows = {r["id"]: r["has_api_key"] for r in list_model_configs(con, limit=10)}
        assert rows == {1: 1, 2: 0, 3: 1}

    # 目的：recent_runs 由 model_config_id 派生 model_name，且非法 status 抛 ValueError。
    # 潜在缺陷：模型名丢失（排查「模型解析失败」时无路由线索），或非法状态静默返回空。
    def test_recent_runs_derives_model_name(self, con: sqlite3.Connection) -> None:
        rows = recent_runs(con, limit=10)
        assert rows[0]["model_config_id"] == 1
        assert rows[0]["model_name"] == "deepseek-flash"
        with pytest.raises(ValueError):
            recent_runs(con, limit=10, status="bogus")

    # 目的：recent_tasks 附带当前 run 状态与派生模型名。潜在缺陷：JOIN 口径错导致状态/模型缺失。
    def test_tasks_join_current_run_and_model(self, con: sqlite3.Connection) -> None:
        rows = recent_tasks(con, limit=10)
        row = next(item for item in rows if item["id"] == 1)
        assert row["current_run_status"] == "completed"
        assert row["current_model_config_id"] == 1
        assert row["current_model_name"] == "deepseek-flash"

    # 目的：recent_tasks contains 对 LIKE 通配符转义，% 不扩大匹配面。潜在缺陷：未转义导致通配。
    def test_tasks_contains_escapes_wildcard(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, "
            "context_window_total, created_at, updated_at) "
            "VALUES (2, 1, 'abc', 'user', 1, 't', 't')"
        )
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, "
            "context_window_total, created_at, updated_at) "
            "VALUES (3, 1, 'a%c', 'user', 1, 't', 't')"
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

    # 目的：model_configs 按 sort_order 升序、limit 生效。潜在缺陷：排序或 limit 失效。
    def test_model_configs_order_and_limit(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO model_configs (id, config_name, base_url, api_key, model_name, "
            "context_window_k, enabled, sort_order) "
            "VALUES (2, 'b', 'u', 'k', 'm2', 32, 1, 5)"
        )
        con.commit()
        assert [r["id"] for r in list_model_configs(con, limit=10)] == [1, 2]
        assert [r["id"] for r in list_model_configs(con, limit=1)] == [1]

    # 目的：commands 可选 task/run 过滤。潜在缺陷：过滤组合错。
    def test_commands_filters(self, con: sqlite3.Connection) -> None:
        con.execute("INSERT INTO conversation_commands VALUES (1,1,'c1','t','h',1,NULL,'t')")
        con.execute("INSERT INTO conversation_commands VALUES (2,1,'c2','t','h',2,NULL,'t')")
        con.commit()
        assert [r["id"] for r in recent_commands(con, limit=10, run_id=2)] == [2]
        assert len(recent_commands(con, limit=10, task_id=1)) == 2


# --------------------------------------------------------------------------- #
# appdb_agent_facts：委派子任务视图
# --------------------------------------------------------------------------- #


class TestChildTasks:
    # 目的：child-tasks 只返回 parent_task_id 命中的子任务，不含父任务自身。潜在缺陷：把父任务当子任务返回。
    def test_child_tasks_filter(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, parent_task_id, "
            "parent_run_id, created_at, updated_at) "
            "VALUES (2, 1, 'child', 'child', 1, 1, 't', 't')"
        )
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, created_at, updated_at) "
            "VALUES (3, 1, 'root', 'user', 't', 't')"
        )
        con.commit()
        assert [r["id"] for r in list_child_tasks(con, limit=10, parent_task_id=1)] == [2]

    # 目的：省略 parent_task_id 时返回所有子任务（parent_task_id 非空）。潜在缺陷：漏掉非空判定，把根任务当子任务。
    def test_child_tasks_all_when_parent_omitted(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, parent_task_id, "
            "created_at, updated_at) VALUES (2, 1, 'child', 'child', 1, 't', 't')"
        )
        con.execute(
            "INSERT INTO tasks (id, workspace_id, title, task_type, created_at, updated_at) "
            "VALUES (3, 1, 'root', 'user', 't', 't')"
        )
        con.commit()
        assert [r["id"] for r in list_child_tasks(con, limit=10)] == [2]


# --------------------------------------------------------------------------- #
# appdb_snapshots
# --------------------------------------------------------------------------- #


class TestSnapshots:
    # 目的：task_snapshot 不存在的 task 抛 ValueError。潜在缺陷：返回 None 泄漏到渲染层。
    def test_task_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            task_snapshot(con, task_id=999, limit=10)

    # 目的：task_snapshot 聚合结构含全部关键块（委派事实已并入 child_tasks）。潜在缺陷：缺失 key 导致渲染崩溃。
    def test_task_snapshot_shape(self, con: sqlite3.Connection) -> None:
        snap = task_snapshot(con, task_id=1, limit=10)
        assert set(snap) == {
            "task",
            "workspace",
            "runs",
            "commands",
            "child_tasks",
            "context",
        }

    # 目的：run_snapshot 不存在的 run 抛 ValueError。潜在缺陷：None 泄漏。
    def test_run_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            run_snapshot(con, run_id=999, limit=10)

    # 目的：run_snapshot 的 task_id 从 run 行取，指向缺失任务时不崩溃，且 run 行带派生模型名。
    # 潜在缺陷：孤儿 run 触发异常，或模型路由线索丢失。
    def test_run_snapshot_orphan_run(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO conversation_runs (id, task_id, input_text, agent_id, "
            "model_config_id, status, created_at, updated_at) "
            "VALUES (9, 777, 'i', 'a', 1, 'completed', 't', 't')"
        )
        con.commit()
        snap = run_snapshot(con, run_id=9, limit=10)
        assert snap["run"]["id"] == 9
        assert snap["run"]["resolved_model_name"] == "deepseek-flash"
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

    # 目的：JSONL 过滤器按最低级别筛选并保持排序契约。潜在缺陷：文件查询遗漏级别。
    def test_filter_entries_min_level(self) -> None:
        entries = [
            {"ts": "1", "level": "INFO", "event": "a"},
            {"ts": "2", "level": "WARNING", "event": "b"},
            {"ts": "3", "level": "ERROR", "event": "c"},
        ]
        result = ql.filter_entries(entries, min_level="WARNING", limit=10)
        assert [entry["event"] for entry in result] == ["b", "c"]

    # 目的：文件查询非法排序抛错。潜在缺陷：排序参数静默失效。
    def test_filter_entries_invalid(self) -> None:
        with pytest.raises(ValueError):
            ql.filter_entries([], order="sideways")

    # 目的：limit 边界校验。潜在缺陷：0/超限被接受。
    @pytest.mark.parametrize("value", [0, -5, ql._MAX_LIMIT + 1])
    def test_limit_invalid(self, value: int) -> None:
        with pytest.raises(ValueError):
            ql.normalize_limit(value)

    # 目的：trace 子命令空 trace_id 抛错。潜在缺陷：空 id 全表返回。
    def test_trace_blank_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["trace", "   ", "--log-file", "missing.log"])
        assert rc == 1

    # 目的：损坏 JSONL 行被跳过而不影响其他记录。潜在缺陷：单行损坏导致整份日志不可查。
    def test_read_entries_skips_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "backend-2026-09-13.log"
        path.write_text('{"event":"ok"}\nnot-json\n', encoding="utf-8")
        assert ql.read_entries([path]) == [{"event": "ok", "data": {}, "error": None, "trace_id": "", "caller": ""}]
