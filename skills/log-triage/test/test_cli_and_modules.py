#!/usr/bin/env python3
"""log-triage CLI 与其查询模块的独立单元测试。

覆盖：参数校验、渲染、schema、agent_facts（含委派子任务视图）、snapshots、
query_logs 的查询构造与边界。业务库用后端 ORM 建表（见 ``app_db_fixture``），不触碰真实库。
"""

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
from app_db_fixture import (  # noqa: E402
    add_run,
    add_task,
    add_task_message,
    add_workspace,
    create_app_db,
)
from appdb_agent_facts import (  # noqa: E402
    ACTIVE_RUN_STATUSES,
    RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    list_child_tasks,
    recent_runs,
    recent_tasks,
    recent_workspaces,
)
from appdb_schema import database_overview, table_detail  # noqa: E402
from appdb_snapshots import run_snapshot, task_snapshot  # noqa: E402


@pytest.fixture()
def con(tmp_path: Path) -> sqlite3.Connection:
    connection = create_app_db(tmp_path / "app.sqlite3")
    add_workspace(connection, name="ws1", root_path="/tmp/ws1")
    add_task(connection, title="t1")
    add_run(connection, task_id=1, end_reason="done", final_output="o", usage_json="{}")
    connection.execute("UPDATE tasks SET current_run_id = 1 WHERE id = 1")
    connection.commit()
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
        assert {"tasks", "workspaces", "conversation_runs", "conversation_task_contexts", "task_messages"} <= names
        assert "model_configs" not in names
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
    # 目的：状态常量与后端 ConversationRunStatus 一致（无 pending），活动态 + 终态 = 全集。
    # 潜在缺陷：skill 的状态白名单与后端漂移，stuck 漏报或 runs --status 拒绝合法值。
    def test_run_status_constants_match_backend(self) -> None:
        from app.models.enums.conversation_run_status import ConversationRunStatus

        assert set(RUN_STATUSES) == {status.value for status in ConversationRunStatus}
        assert set(ACTIVE_RUN_STATUSES) | set(TERMINAL_RUN_STATUSES) == set(RUN_STATUSES)
        assert not set(ACTIVE_RUN_STATUSES) & set(TERMINAL_RUN_STATUSES)

    # 目的：recent_runs 原样带出「连接 id/模型名」，且非法 status 抛 ValueError。
    # 潜在缺陷：模型路由线索丢失，或非法状态静默返回空。
    def test_recent_runs_model_config_id_and_status(self, con: sqlite3.Connection) -> None:
        rows = recent_runs(con, limit=10)
        assert rows[0]["model_config_id"] == "deepseek/deepseek-flash"
        with pytest.raises(ValueError):
            recent_runs(con, limit=10, status="pending")

    # 目的：recent_runs contains 能按模型 id 搜索。潜在缺陷：搜索面漏掉模型列。
    def test_recent_runs_contains_model(self, con: sqlite3.Connection) -> None:
        add_run(con, task_id=1, model_config_id="other/gpt")
        assert [r["id"] for r in recent_runs(con, limit=10, contains="deepseek")] == [1]

    # 目的：recent_tasks 附带当前 run 状态与模型 id。潜在缺陷：JOIN 口径错导致状态/模型缺失。
    def test_tasks_join_current_run(self, con: sqlite3.Connection) -> None:
        rows = recent_tasks(con, limit=10)
        row = next(item for item in rows if item["id"] == 1)
        assert row["current_run_status"] == "completed"
        assert row["current_model_config_id"] == "deepseek/deepseek-flash"
        assert "tool_schemas" not in row

    # 目的：recent_tasks contains 对 LIKE 通配符转义，% 不扩大匹配面。潜在缺陷：未转义导致通配。
    def test_tasks_contains_escapes_wildcard(self, con: sqlite3.Connection) -> None:
        add_task(con, title="abc")
        add_task(con, title="a%c")
        rows = recent_tasks(con, limit=10, contains="%")
        assert [r["title"] for r in rows] == ["a%c"]

    # 目的：workspaces 按 id 降序、limit 生效。潜在缺陷：排序错。
    def test_workspaces_order(self, con: sqlite3.Connection) -> None:
        add_workspace(con, name="ws2", root_path="/x")
        assert [r["id"] for r in recent_workspaces(con, limit=10)] == [2, 1]
        assert [r["id"] for r in recent_workspaces(con, limit=1)] == [2]


# --------------------------------------------------------------------------- #
# appdb_agent_facts：委派子任务视图
# --------------------------------------------------------------------------- #


class TestChildTasks:
    # 目的：child-tasks 只返回 parent_task_id 命中的子任务，不含父任务自身。潜在缺陷：把父任务当子任务返回。
    def test_child_tasks_filter(self, con: sqlite3.Connection) -> None:
        add_task(con, title="child", task_type="agent_task", parent_task_id=1, derivation_depth=1)
        add_task(con, title="root")
        rows = list_child_tasks(con, limit=10, parent_task_id=1)
        assert [r["id"] for r in rows] == [2]
        assert rows[0]["derivation_depth"] == 1

    # 目的：省略 parent_task_id 时返回所有子任务（parent_task_id 非空）。潜在缺陷：把根任务当子任务。
    def test_child_tasks_all_when_parent_omitted(self, con: sqlite3.Connection) -> None:
        add_task(con, title="child", task_type="agent_task", parent_task_id=1)
        add_task(con, title="root")
        assert [r["id"] for r in list_child_tasks(con, limit=10)] == [2]


# --------------------------------------------------------------------------- #
# appdb_snapshots
# --------------------------------------------------------------------------- #


class TestSnapshots:
    # 目的：task_snapshot 不存在的 task 抛 ValueError。潜在缺陷：返回 None 泄漏到渲染层。
    def test_task_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            task_snapshot(con, task_id=999, limit=10)

    # 目的：task_snapshot 聚合结构含全部关键块，task 行不带整份 tool_schemas。潜在缺陷：缺失 key 导致渲染崩溃。
    def test_task_snapshot_shape(self, con: sqlite3.Connection) -> None:
        snap = task_snapshot(con, task_id=1, limit=10)
        assert set(snap) == {
            "task",
            "workspace",
            "runs",
            "child_tasks",
            "queued_messages",
            "context",
        }
        assert "tool_schemas" not in snap["task"]

    # 目的：task 快照列出本 Task 外部队列积压（种类/投递方式），不带正文，不串到其它 Task。
    # 潜在缺陷：排队消息看不到，「发了没反应」无从排查；或正文泄漏到快照。
    def test_task_snapshot_queued_messages(self, con: sqlite3.Connection) -> None:
        add_task(con, title="other")
        add_task_message(con, task_id=1, kind="user_input", delivery="next_run", payload={"text": "secret"})
        add_task_message(con, task_id=1, kind="system_notice", delivery="immediate")
        add_task_message(con, task_id=2)
        queued = task_snapshot(con, task_id=1, limit=10)["queued_messages"]
        assert [(q["kind"], q["delivery"]) for q in queued] == [
            ("user_input", "next_run"),
            ("system_notice", "immediate"),
        ]
        assert "secret" not in json.dumps(queued)

    # 目的：run_snapshot 不存在的 run 抛 ValueError。潜在缺陷：None 泄漏。
    def test_run_not_found(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            run_snapshot(con, run_id=999, limit=10)

    # 目的：run_snapshot 的 task_id 从 run 行取，指向缺失任务时不崩溃，且 run 行带模型 id。
    # 潜在缺陷：孤儿 run 触发异常，或模型路由线索丢失。
    def test_run_snapshot_orphan_run(self, con: sqlite3.Connection) -> None:
        add_run(con, task_id=777, model_config_id="conn/m")
        snap = run_snapshot(con, run_id=2, limit=10)
        assert snap["run"]["id"] == 2
        assert snap["run"]["model_config_id"] == "conn/m"
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
