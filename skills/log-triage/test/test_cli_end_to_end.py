#!/usr/bin/env python3
"""log-triage 两个 CLI 的端到端测试（内存/临时库，不触碰真实库）。

通过 main(argv) 直接驱动 CLI，覆盖参数面、分发、渲染、退出码与 --save。

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


def _build_app_db(path: Path) -> None:
    con = sqlite3.connect(path)
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
    con.execute(
        "INSERT INTO tasks VALUES (1,1,'中文任务😀','chat',NULL,NULL,1,NULL,0,100,'t','t',NULL)"
    )
    con.execute(
        "INSERT INTO conversation_runs VALUES (1,1,'completed','a',1,'m',NULL,'done','i','o','{}','{}','t','t',NULL)"
    )
    import json as _json

    con.execute(
        "INSERT INTO conversation_task_contexts VALUES (1,1,1,NULL,?, '{}',1,1,'t','t',0)",
        (_json.dumps({"type": "human", "data": {"content": "hello 世界"}}),),
    )
    con.commit()
    con.close()


@pytest.fixture()
def app_db(tmp_path: Path) -> str:
    p = tmp_path / "app.sqlite3"
    _build_app_db(p)
    return str(p)


APP_SUBCOMMANDS = [
    ["schema", "--no-columns"],
    ["workspaces"],
    ["tasks", "--limit", "5"],
    ["tasks", "--contains", "中文"],
    ["runs", "--status", "completed"],
    ["run", "1"],
    ["task", "1"],
    ["commands"],
    ["messages", "1"],
    ["messages", "1", "--exclude-streaming"],
    ["tools", "1"],
    ["delegations"],
    ["sessions"],
    ["attachments"],
    ["providers"],
    ["models"],
    ["stuck"],
]


class TestAppDbCli:
    # 目的：16 个子命令全部退出码 0 且无 traceback。潜在缺陷：某子命令未接线或崩溃。
    @pytest.mark.parametrize("cmd", APP_SUBCOMMANDS)
    def test_all_subcommands_ok(
        self, app_db: str, cmd: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = qad.main([*cmd, "--db", app_db, "--format", "json"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Traceback" not in captured.err

    # 目的：缺库文件返回码为 1 且错误文本清晰。潜在缺陷：FileNotFoundError 外泄堆栈。
    def test_missing_db_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        rc = qad.main(["tasks", "--db", str(tmp_path / "nope.sqlite3")])
        assert rc == 1
        assert "not found" in capsys.readouterr().err

    # 目的：非法 limit 返回码 1（用户错误）。潜在缺陷：limit=0 静默通过。
    def test_invalid_limit(self, app_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = qad.main(["tasks", "--limit", "0", "--db", app_db])
        assert rc == 1
        assert "limit" in capsys.readouterr().err

    # 目的：--save 写出文件且内容为合法 JSON；已存在未 --force 返回码 1。潜在缺陷：静默覆盖。
    def test_save_and_force(
        self, app_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "out.json"
        rc = qad.main(["tasks", "--db", app_db, "--format", "json", "--save", str(out)])
        assert rc == 0
        assert json.loads(out.read_text(encoding="utf-8"))
        rc2 = qad.main(["tasks", "--db", app_db, "--format", "json", "--save", str(out)])
        assert rc2 == 1
        rc3 = qad.main(["tasks", "--db", app_db, "--format", "json", "--save", str(out), "--force"])
        assert rc3 == 0

    # 目的：中文与表情符号能正常渲染（GBK 控制台不崩）。潜在缺陷：UnicodeEncodeError。
    def test_unicode_output(self, app_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = qad.main(["tasks", "--db", app_db])
        captured = capsys.readouterr()
        assert rc == 0
        assert "中文任务" in captured.out


def _build_log_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE log_entries (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, logger TEXT,"
        " trace_id TEXT, caller TEXT, event TEXT, msg TEXT, data_json TEXT, error_json TEXT, truncated BOOLEAN)"
    )
    rows = [
        (
            1,
            "2026-09-13T10:00:00.000Z",
            "INFO",
            "l",
            "t1",
            "c",
            "evt_a",
            "msg 中文😀",
            '{"k":1}',
            None,
            0,
        ),
        (
            2,
            "2026-09-13T10:01:00.000Z",
            "ERROR",
            "l",
            "t1",
            "c",
            "evt_b",
            "boom",
            "{}",
            '{"type":"X"}',
            1,
        ),
        (3, "2026-09-13T10:02:00.000Z", "WARNING", "l", "t2", "c", "evt_c", "warn", "{}", None, 0),
    ]
    con.executemany("INSERT INTO log_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


@pytest.fixture()
def log_db(tmp_path: Path) -> str:
    p = tmp_path / "logs.sqlite3"
    _build_log_db(p)
    return str(p)


class TestLogCli:
    # 目的：trace 子命令返回该 trace 的全部条目并退出 0。潜在缺陷：trace 过滤失效。
    def test_trace(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["trace", "t1", "--db", log_db, "--format", "json"])
        assert rc == 0
        entries = json.loads(capsys.readouterr().out)
        assert {e["event"] for e in entries} == {"evt_a", "evt_b"}

    # 目的：--errors-only 只返回 ERROR 及以上。潜在缺陷：级别过滤失效。
    def test_errors_only(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--errors-only", "--db", log_db, "--format", "json"])
        assert rc == 0
        entries = json.loads(capsys.readouterr().out)
        assert [e["level"] for e in entries] == ["ERROR"]

    # 目的：无匹配时 recent 输出 (no entries) 且退出 0。潜在缺陷：空结果崩溃。
    def test_no_entries(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--contains", "zzzz-no-match", "--db", log_db])
        assert rc == 0
        assert "(no entries)" in capsys.readouterr().out

    # 目的：多级别开关冲突返回码 1。潜在缺陷：冲突被忽略。
    def test_conflicting_levels(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--errors-only", "--warnings-up", "--db", log_db])
        assert rc == 1

    # 目的：中文与表情符号日志正常渲染。潜在缺陷：UnicodeEncodeError。
    def test_unicode(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--db", log_db])
        assert rc == 0
        assert "中文😀" in capsys.readouterr().out

    # 目的：--save 写出文件；重复写未 --force 返回码 1。潜在缺陷：静默覆盖。
    def test_save(self, log_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out = tmp_path / "logs.json"
        rc = ql.main(["recent", "--db", log_db, "--format", "json", "--save", str(out)])
        assert rc == 0
        assert len(json.loads(out.read_text(encoding="utf-8"))) == 3
        rc2 = ql.main(["recent", "--db", log_db, "--format", "json", "--save", str(out)])
        assert rc2 == 1

    # 目的：非法 level 返回码 1。潜在缺陷：非法级别进入 SQL。
    def test_invalid_level(self, log_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--level", "TRACE", "--db", log_db])
        assert rc == 1
