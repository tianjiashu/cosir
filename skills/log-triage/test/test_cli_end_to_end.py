#!/usr/bin/env python3
"""log-triage 两个 CLI 的端到端测试（临时库用后端 ORM 建表，不触碰真实库）。

通过 main(argv) 直接驱动 CLI，覆盖参数面、分发、渲染、退出码与 --save。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import query_app_db as qad  # noqa: E402
import query_logs as ql  # noqa: E402
from app_db_fixture import (  # noqa: E402
    add_context,
    add_run,
    add_task,
    add_task_message,
    add_workspace,
    assistant_message,
    create_app_db,
    tool_message,
    user_message,
)


def _build_app_db(path: Path) -> None:
    """一个 Task 两个 Run：run 1 完成，run 2 挂起在后台命令上；外加一条排队消息。"""

    con = create_app_db(path)
    add_workspace(con, name="ws1", root_path="/tmp/ws1")
    add_task(con, title="中文任务😀")
    add_run(con, task_id=1, end_reason="done", final_output="o", usage_json="{}")
    add_run(con, task_id=1, status="suspended")
    con.execute("UPDATE tasks SET current_run_id = 2 WHERE id = 1")
    con.commit()
    add_context(con, task_id=1, run_id=1, sequence=1, message=user_message("hello 世界"), metadata={"kind": "user_input"})
    add_context(
        con, task_id=1, run_id=1, sequence=2,
        message=assistant_message("", ("c1", "read_file", {"path": "a.py"}), ("c2", "web_search", {"q": "x"})),
        metadata={"model": "deepseek-flash", "finish_reason": "tool_calls", "usage": {"total_tokens": 9}},
    )
    add_context(con, task_id=1, run_id=1, sequence=3, message=tool_message("c1", "print(1)"), metadata={"status": "completed"})
    # c2 没有结果行：调用后进程被强杀。
    add_context(
        con, task_id=1, run_id=2, sequence=4, turn=2,
        message=assistant_message("", ("c3", "execute_terminal", {"command": "sleep 100"})),
        metadata={"finish_reason": "tool_calls"},
    )
    add_context(
        con, task_id=1, run_id=2, sequence=5, turn=2,
        message=tool_message("c3", "running in background"),
        metadata={"status": "pending", "background": {"label": "sleep"}},
    )
    # 被中断的流式回复：不进上下文。
    add_context(con, task_id=1, run_id=2, sequence=6, turn=2, message=assistant_message("半截回复"), include_in_context=0)
    add_task_message(con, task_id=1, kind="user_input", delivery="next_run")
    con.close()


@pytest.fixture()
def app_db(tmp_path: Path) -> str:
    p = tmp_path / "app.sqlite3"
    _build_app_db(p)
    return str(p)


def _json(cmd: list[str], db: str, capsys: pytest.CaptureFixture[str]) -> object:
    rc = qad.main([*cmd, "--db", db, "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return json.loads(captured.out)


APP_SUBCOMMANDS = [
    ["schema", "--no-columns"],
    ["workspaces"],
    ["tasks", "--limit", "5"],
    ["tasks", "--contains", "中文"],
    ["runs", "--status", "completed"],
    ["runs", "--status", "suspended"],
    ["run", "1"],
    ["task", "1"],
    ["messages", "1"],
    ["messages", "1", "--in-context-only"],
    ["tools", "1"],
    ["tools", "1", "--failures-only"],
    ["child-tasks"],
    ["stuck"],
]


class TestAppDbCli:
    # 目的：全部子命令退出码 0 且无 traceback。潜在缺陷：某子命令未接线或崩溃。
    @pytest.mark.parametrize("cmd", APP_SUBCOMMANDS)
    def test_all_subcommands_ok(
        self, app_db: str, cmd: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = qad.main([*cmd, "--db", app_db, "--format", "json"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Traceback" not in captured.err

    # 目的：已删的子命令与旧参数被参数解析拒绝。潜在缺陷：旧入口残留，引导排查者去查不存在的事实。
    @pytest.mark.parametrize("cmd", [["model-configs"], ["messages", "1", "--exclude-streaming"]])
    def test_removed_entries_rejected(self, app_db: str, cmd: list[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            qad.main([*cmd, "--db", app_db])
        assert excinfo.value.code != 0

    # 目的：--in-context-only 只留进模型上下文的行，排除被中断的流式回复。潜在缺陷：过滤失效或误删正常行。
    def test_messages_in_context_only(self, app_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        everything = _json(["messages", "1"], app_db, capsys)
        in_context = _json(["messages", "1", "--in-context-only"], app_db, capsys)
        assert [m["sequence"] for m in everything] == [1, 2, 3, 4, 5, 6]
        assert [m["sequence"] for m in in_context] == [1, 2, 3, 4, 5]
        assert everything[-1]["include_in_context"] is False
        assert everything[0]["kind"] == "user_input"
        assert everything[1]["tool_calls"][0] == {"id": "c1", "name": "read_file", "args": {"path": "a.py"}}

    # 目的：tools 按 tool_call_id 配对，状态取结果行 metadata；没有结果行为 no_result，后台占位带 background。
    # 潜在缺陷：配对错位、缺结果被说成 pending、后台命令与 HITL 等待分不开。
    def test_tools_pairing(self, app_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        rows = {r["tool_call_id"]: r for r in _json(["tools", "1"], app_db, capsys)}
        assert rows["c1"]["status"] == "completed"
        assert rows["c1"]["result_summary"] == "print(1)"
        assert rows["c2"]["status"] == "no_result"
        assert rows["c2"]["result_sequence"] is None
        assert rows["c3"]["status"] == "pending"
        assert rows["c3"]["background"] == {"label": "sleep"}
        assert (rows["c3"]["run_id"], rows["c3"]["turn"]) == (2, 2)
        assert _json(["tools", "1", "--run-id", "2"], app_db, capsys)[0]["tool_call_id"] == "c3"

    # 目的：stuck 报出挂起的 run、指向它的 task、未结算的后台占位与排队积压。潜在缺陷：任一类漏报。
    def test_stuck(self, app_db: str, capsys: pytest.CaptureFixture[str]) -> None:
        out = _json(["stuck"], app_db, capsys)
        assert [r["id"] for r in out["active_runs"]] == [2]
        assert [t["id"] for t in out["tasks_with_active_run"]] == [1]
        [placeholder] = out["open_tool_placeholders"]
        assert placeholder["tool_call_id"] == "c3"
        assert placeholder["is_background"] == 1
        assert placeholder["run_status"] == "suspended"
        [queued] = out["queued_messages"]
        assert (queued["task_id"], queued["queued"], queued["kinds"]) == (1, 1, "user_input")

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


@pytest.fixture()
def log_file(tmp_path: Path) -> str:
    p = tmp_path / "backend-2026-09-13.log"
    rows = [
        {"ts": "2026-09-13T10:00:00.000Z", "level": "INFO", "logger": "l", "trace_id": "t1", "caller": "c", "event": "evt_a", "msg": "msg 中文😀", "data": {"k": 1}, "error": None, "truncated": False},
        {"ts": "2026-09-13T10:01:00.000Z", "level": "ERROR", "logger": "l", "trace_id": "t1", "caller": "c", "event": "evt_b", "msg": "boom", "data": {}, "error": {"type": "X"}, "truncated": True},
        {"ts": "2026-09-13T10:02:00.000Z", "level": "WARNING", "logger": "l", "trace_id": "t2", "caller": "c", "event": "evt_c", "msg": "warn", "data": {}, "error": None, "truncated": False},
    ]
    p.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return str(p)


class TestLogCli:
    # 目的：trace 子命令返回该 trace 的全部条目并退出 0。潜在缺陷：trace 过滤失效。
    def test_trace(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["trace", "t1", "--log-file", log_file, "--format", "json"])
        assert rc == 0
        entries = json.loads(capsys.readouterr().out)
        assert {e["event"] for e in entries} == {"evt_a", "evt_b"}

    # 目的：--errors-only 只返回 ERROR 及以上。潜在缺陷：级别过滤失效。
    def test_errors_only(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--errors-only", "--log-file", log_file, "--format", "json"])
        assert rc == 0
        entries = json.loads(capsys.readouterr().out)
        assert [e["level"] for e in entries] == ["ERROR"]

    # 目的：无匹配时 recent 输出 (no entries) 且退出 0。潜在缺陷：空结果崩溃。
    def test_no_entries(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--contains", "zzzz-no-match", "--log-file", log_file])
        assert rc == 0
        assert "(no entries)" in capsys.readouterr().out

    # 目的：多级别开关冲突返回码 1。潜在缺陷：冲突被忽略。
    def test_conflicting_levels(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--errors-only", "--warnings-up", "--log-file", log_file])
        assert rc == 1

    # 目的：中文与表情符号日志正常渲染。潜在缺陷：UnicodeEncodeError。
    def test_unicode(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--log-file", log_file])
        assert rc == 0
        assert "中文😀" in capsys.readouterr().out

    # 目的：--save 写出文件；重复写未 --force 返回码 1。潜在缺陷：静默覆盖。
    def test_save(self, log_file: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out = tmp_path / "logs.json"
        rc = ql.main(["recent", "--log-file", log_file, "--format", "json", "--save", str(out)])
        assert rc == 0
        assert len(json.loads(out.read_text(encoding="utf-8"))) == 3
        rc2 = ql.main(["recent", "--log-file", log_file, "--format", "json", "--save", str(out)])
        assert rc2 == 1

    # 目的：非法 level 返回码 1。潜在缺陷：非法级别进入 SQL。
    def test_invalid_level(self, log_file: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = ql.main(["recent", "--level", "TRACE", "--log-file", log_file])
        assert rc == 1
