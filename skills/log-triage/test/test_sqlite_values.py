#!/usr/bin/env python3
"""log-triage 查询脚本的独立单元测试（第三轮对抗性复测）。

只测行为契约：布尔归一（Python/SQL 双侧一致性）、消息回放流式过滤口径、
stuck 残留草稿判定、工具调用配对、缺表可诊断性、只读与参数边界。

不修改任何生产代码；所有用例走临时库或内存库。

注：``# ruff: noqa: E501`` —— 建表与造数用的一行 SQL/JSON 字面量天然超长，
拆行会显著降低夹具可读性且无收益，故对本文件整体豁免行宽。
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from appdb_context import context_statistics, list_messages, summarize_tool_calls  # noqa: E402
from appdb_health import find_unsettled  # noqa: E402
from appdb_readonly import (  # noqa: E402
    contains_pattern,
    escape_like,
    require_tables,
    safe_json,
)
from sqlite_values import BOOL_FALSE_TEXTS, as_bool, bool_true_sql  # noqa: E402

# --------------------------------------------------------------------------- #
# sqlite_values：Python 侧 as_bool
# --------------------------------------------------------------------------- #


class TestAsBool:
    """as_bool 的取值归一契约。"""

    # 目的：None/空串/0/'0'/'false' 等一律为 False；确证假值集合无遗漏。潜在缺陷：漏判导致 false 被当 true。
    @pytest.mark.parametrize(
        "value",
        [
            None,
            0,
            0.0,
            "",
            "0",
            "false",
            "FALSE",
            "False",
            " false ",
            "no",
            "NO",
            "off",
            "OFF",
            "none",
            "null",
            "NONE",
            "  ",
            False,
        ],
    )
    def test_falsey_values(self, value: object) -> None:
        assert as_bool(value) is False

    # 目的：1/'1'/'true'/'yes'/任意非假文本一律 True；确证真值集合。潜在缺陷：误把 'yes' 当假。
    @pytest.mark.parametrize(
        "value",
        [
            1,
            2,
            -1,
            1.0,
            "1",
            "true",
            "TRUE",
            "True",
            "yes",
            "on",
            "2",
            "-1",
            "abc",
            "0x1",
            " 1 ",
            True,
        ],
    )
    def test_truthy_values(self, value: object) -> None:
        assert as_bool(value) is True

    # 目的：bool 短路优先于 int（bool 是 int 子类），确证 True/False 原样返回。潜在缺陷：isinstance 顺序错误。
    def test_bool_not_reinterpreted_as_int(self) -> None:
        assert as_bool(True) is True
        assert as_bool(False) is False

    # 目的：非常规对象落到 bool(value)；确证 fallback 分支存在且不抛。潜在缺陷：任意对象触发异常。
    def test_fallback_object(self) -> None:
        assert as_bool([]) is False
        assert as_bool([1]) is True


# --------------------------------------------------------------------------- #
# sqlite_values：SQL 侧 bool_true_sql 与 Python 侧等价性
# --------------------------------------------------------------------------- #

_VALUES = [
    None,
    0,
    1,
    "",
    "0",
    "1",
    "false",
    "FALSE",
    " false ",
    "no",
    "yes",
    "2",
    "-1",
    "abc",
    "off",
    "on",
    "none",
    "null",
    "True",
    "  ",
    " 1 ",
    True,
    False,
    1.0,
    2,
    -1,
    # REAL 形态的数值零：CAST 文本是 '0.0'，必须与 as_bool(0.0) is False 一致（历史缺陷点）。
    0.0,
    -0.0,
    0.5,
]


class TestBoolSqlEquivalence:
    """bool_true_sql 必须与 as_bool 在同一批取值上语义等价。"""

    # 目的：同批取值分别走 Python 与 SQL，交叉比对必须完全一致。潜在缺陷：TEXT/REAL 形态下两侧口径分叉。
    # 夹具说明：表达式会多次引用列，因此必须用**真实列名**（`v`）+ 单占位符内联视图，
    # 不能用 `?` 当"列名"（占位符数量会与绑定参数个数不匹配）。
    @pytest.mark.parametrize("value", _VALUES)
    def test_python_sql_agree(self, value: object) -> None:
        con = sqlite3.connect(":memory:")
        try:
            sql = f"SELECT ({bool_true_sql('v')}) FROM (SELECT ? AS v)"  # noqa: S608 - 表达式片段来自内部常量
            row = con.execute(sql, (value,)).fetchone()
            sql_bool = None if row[0] is None else bool(row[0])
        finally:
            con.close()
        assert sql_bool == as_bool(value)

    # 目的：NULL 在 SQL 侧不得因三值逻辑被误判；确证 COALESCE 兜底。潜在缺陷：NULL 使 NOT IN 求值为 NULL 丢行。
    def test_null_is_false_in_sql(self) -> None:
        con = sqlite3.connect(":memory:")
        try:
            row = con.execute(
                f"SELECT ({bool_true_sql('v')}) FROM (SELECT ? AS v)",  # noqa: S608 - 同上
                (None,),
            ).fetchone()
        finally:
            con.close()
        assert bool(row[0]) is False

    # 目的：表达式片段必须为自洽合法 SQL，且假值集合来自唯一常量。潜在缺陷：拼接非法或漏 TS。
    def test_expression_contains_all_false_literals(self) -> None:
        expr = bool_true_sql("c.is_streaming")
        for item in BOOL_FALSE_TEXTS:
            assert f"'{item}'" in expr


# --------------------------------------------------------------------------- #
# 临时库构造工具
# --------------------------------------------------------------------------- #


def _base_schema(con: sqlite3.Connection) -> None:
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
    con.execute(
        "INSERT INTO conversation_runs VALUES (2,1,'running','a',1,'m',NULL,NULL,'i',NULL,'{}','{}','t','t',NULL)"
    )
    con.commit()


@pytest.fixture()
def con() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _base_schema(connection)
    yield connection
    connection.close()


def _msg(kind: str, data: dict) -> str:
    import json

    return json.dumps({"type": kind, "data": data}, ensure_ascii=False)


def _add(
    con: sqlite3.Connection,
    task: int,
    run: int | None,
    seq: int,
    is_streaming: object,
    mj: str,
    tool_call_id: str | None = None,
    tmeta: str = "{}",
) -> None:
    con.execute(
        "INSERT INTO conversation_task_contexts (task_id,run_id,tool_call_id,message_json,"
        "transport_metadata_json,include_in_context,sequence,created_at,updated_at,is_streaming)"
        " VALUES (?,?,?,?,?,1,?,?,?,?)",
        (task, run, tool_call_id, mj, tmeta, seq, "t", "t", is_streaming),
    )
    con.commit()


# --------------------------------------------------------------------------- #
# list_messages：--exclude-streaming 过滤与显示自洽性
# --------------------------------------------------------------------------- #


class TestListMessagesStreamingFilter:
    """messages 显示的 is_streaming 与 --exclude-streaming 过滤口径必须自洽。"""

    # 目的：TEXT 假值（'false'/'no'/'FALSE'/'0'/NULL/''）不得被 --exclude-streaming 误删。潜在缺陷：裸比较整行漏掉。
    @pytest.mark.parametrize("stored", ["false", "no", "FALSE", "0", None, ""])
    def test_falsey_text_is_kept(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, 1, 1, stored, _msg("human", {"content": "keep-me"}))
        rows = list_messages(con, task_id=1, limit=50, include_streaming=False)
        assert len(rows) == 1
        assert rows[0]["is_streaming"] is False

    # 目的：真值（'true'/'1'/1）必须被 --exclude-streaming 排除，且显示侧为 True。潜在缺陷：TEXT 'true' 漏排除。
    @pytest.mark.parametrize("stored", ["true", "TRUE", "1", 1])
    def test_truthy_is_excluded(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, 1, 1, stored, _msg("ai", {"content": "drop-me"}))
        assert list_messages(con, task_id=1, limit=50, include_streaming=False) == []
        shown = list_messages(con, task_id=1, limit=50, include_streaming=True)
        assert len(shown) == 1
        assert shown[0]["is_streaming"] is True

    # 目的：同一行在「显示」与「过滤」两侧必须自洽——显示 False 的行绝不能被当流式删掉。潜在缺陷：两侧口径分叉。
    @pytest.mark.parametrize("stored", ["false", "no", "0", None, "", "true", "1", 1, 0])
    def test_display_and_filter_self_consistent(
        self, con: sqlite3.Connection, stored: object
    ) -> None:
        _add(con, 1, 1, 1, stored, _msg("human", {"content": "x"}))
        shown = list_messages(con, task_id=1, limit=50, include_streaming=True)
        filtered = list_messages(con, task_id=1, limit=50, include_streaming=False)
        displayed_is_streaming = shown[0]["is_streaming"]
        # 显示为 False => 过滤后必须仍在；显示为 True => 过滤后必须消失
        if displayed_is_streaming is False:
            assert len(filtered) == 1
        else:
            assert filtered == []

    # 目的：非法 order 抛 ValueError。潜在缺陷：静默接受导致 SQL 注入式拼接。
    def test_invalid_order_raises(self, con: sqlite3.Connection) -> None:
        with pytest.raises(ValueError):
            list_messages(con, task_id=1, limit=10, order="sideways")

    # 目的：缺表抛带表清单的 ValueError。潜在缺陷：抛出裸 OperationalError。
    def test_missing_table_raises_value_error(self) -> None:
        bare = sqlite3.connect(":memory:")
        bare.row_factory = sqlite3.Row
        try:
            with pytest.raises(ValueError) as excinfo:
                list_messages(bare, task_id=1, limit=10)
            assert "table not found" in str(excinfo.value)
            assert "tables in this database" in str(excinfo.value)
        finally:
            bare.close()

    # 目的：desc 顺序 + limit 与 include_streaming=False 组合，确认 LIMIT 在过滤之后生效（无假值被挤掉）。潜在缺陷：过滤前 LIMIT 导致假值行被真值挤占。
    def test_desc_order_filter_before_limit(self, con: sqlite3.Connection) -> None:
        for seq in range(1, 6):
            _add(con, 1, 1, seq, "true", _msg("ai", {"content": f"draft{seq}"}))
        _add(con, 1, 1, 6, "0", _msg("human", {"content": "real"}))
        rows = list_messages(con, task_id=1, limit=5, order="desc", include_streaming=False)
        assert [r["text"] for r in rows] == ["real"]

    # 目的：run_id 过滤与 task_id 联合，确认不串行到其它 run。潜在缺陷：run 过滤被忽略。
    def test_run_id_scoping(self, con: sqlite3.Connection) -> None:
        _add(con, 1, 1, 1, 0, _msg("human", {"content": "r1"}))
        _add(con, 1, 2, 2, 0, _msg("human", {"content": "r2"}))
        rows = list_messages(con, task_id=1, run_id=2, limit=10)
        assert [r["text"] for r in rows] == ["r2"]


# --------------------------------------------------------------------------- #
# context_statistics：流式草稿计数口径
# --------------------------------------------------------------------------- #


class TestContextStatistics:
    """context_statistics 的 is_streaming/include_in_context 计数口径。"""

    # 目的：TEXT 'true' 形态的流式草稿必须计入 streaming_drafts。潜在缺陷：as_bool 未用于计数导致漏计。
    def test_streaming_drafts_counts_text_true(self, con: sqlite3.Connection) -> None:
        _add(con, 1, 1, 1, "true", _msg("ai", {"content": "d"}))
        _add(con, 1, 1, 2, " 1 ", _msg("ai", {"content": "d"}))
        _add(con, 1, 1, 3, "false", _msg("human", {"content": "h"}))
        stats = context_statistics(con, task_id=1)
        assert stats["streaming_drafts"] == 2
        assert stats["total"] == 3

    # 目的：by_kind 对损坏 message_json 归为 unknown 且不抛。潜在缺陷：JSON 解析失败导致异常。
    def test_corrupt_json_is_unknown_kind(self, con: sqlite3.Connection) -> None:
        _add(con, 1, 1, 1, 0, "{not json")
        stats = context_statistics(con, task_id=1)
        assert stats["by_kind"] == {"unknown": 1}


# --------------------------------------------------------------------------- #
# find_unsettled：残留流式草稿判定
# --------------------------------------------------------------------------- #


class TestFindUnsettled:
    """stuck 的残留流式草稿判定。"""

    # 目的：终态 run 下 TEXT 'true'/'TRUE'/' 1 ' 草稿必须报出。潜在缺陷：TEXT 真值漏报。
    @pytest.mark.parametrize("stored", ["true", "TRUE", " 1 ", "1", 1])
    def test_stale_draft_true_reported(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, 1, 1, stored, _msg("ai", {"content": "stale"}))
        result = find_unsettled(con, limit=50)
        assert len(result["stale_streaming_drafts"]) == 1

    # 目的：TEXT 'false'/'0'/NULL 草稿在终态 run 下不得误报。潜在缺陷：裸比较把假值当流式。
    @pytest.mark.parametrize("stored", ["false", "0", None, "", "no"])
    def test_stale_draft_falsey_not_reported(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, 1, 1, stored, _msg("human", {"content": "fine"}))
        result = find_unsettled(con, limit=50)
        assert result["stale_streaming_drafts"] == []

    # 目的：活跃 run（running）下的流式草稿不算残留（run 未终态）。潜在缺陷：把活跃 run 草稿误报为残留。
    def test_active_run_draft_not_stale(self, con: sqlite3.Connection) -> None:
        _add(con, 1, 2, 1, "true", _msg("ai", {"content": "live"}))  # run 2 = running
        result = find_unsettled(con, limit=50)
        assert result["stale_streaming_drafts"] == []

    # 目的：run 不存在（run_id 指向缺失行）的流式草稿算残留。潜在缺陷：LEFT JOIN NULL 分支漏判。
    def test_orphan_run_draft_is_stale(self, con: sqlite3.Connection) -> None:
        _add(con, 1, 999, 1, "true", _msg("ai", {"content": "ghost"}))
        result = find_unsettled(con, limit=50)
        assert len(result["stale_streaming_drafts"]) == 1

    # 目的：活跃 run 必须被列出，终态 run 不列出。潜在缺陷：状态白名单错。
    def test_active_runs_listed(self, con: sqlite3.Connection) -> None:
        result = find_unsettled(con, limit=50)
        ids = {r["id"] for r in result["active_runs"]}
        assert ids == {2}

    # 目的：stuck 缺表必须抛带清单的 ValueError。潜在缺陷：裸 OperationalError。
    def test_missing_table_raises(self) -> None:
        bare = sqlite3.connect(":memory:")
        try:
            with pytest.raises(ValueError) as excinfo:
                find_unsettled(bare, limit=10)
            assert "table not found" in str(excinfo.value)
        finally:
            bare.close()


# --------------------------------------------------------------------------- #
# summarize_tool_calls：配对回归
# --------------------------------------------------------------------------- #


class TestToolCallPairing:
    """工具调用与结果的配对语义。"""

    # 目的：无 id 的工具调用不产条目（对齐后端无需配对语义）。潜在缺陷：无 id 调用被渲染成永久 pending。
    def test_idless_call_no_entry(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"name": "no_id", "args": {}}]}),
        )
        assert summarize_tool_calls(con, task_id=1, limit=50) == []

    # 目的：无 id 与有 id 混合时只产有 id 条目。潜在缺陷：整条 AI 消息被丢弃。
    def test_mixed_idless_and_ided(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg(
                "ai",
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "no_id", "args": {}},
                        {"id": "c1", "name": "ok", "args": {}},
                    ],
                },
            ),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("tool", {"content": "r", "tool_call_id": "c1", "status": "success"}),
            tool_call_id="c1",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [r["tool_call_id"] for r in rows] == ["c1"]
        assert rows[0]["status"] == "success"

    # 目的：空 id 字符串（''）与缺 id 同义，不产条目。潜在缺陷：'' 被当有效 id。
    def test_empty_string_id_no_entry(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "", "name": "e", "args": {}}]}),
        )
        assert summarize_tool_calls(con, task_id=1, limit=50) == []

    # 目的：跨三个 run 复用同一 tool_call_id，各自配对自己的结果（FIFO）。潜在缺陷：结果串到后续 run。
    def test_cross_run_reuse_three_runs(self, con: sqlite3.Connection) -> None:
        for run, base in ((1, 10), (2, 20), (3, 30)):
            _add(
                con,
                1,
                run,
                base,
                0,
                _msg(
                    "ai",
                    {
                        "content": "",
                        "tool_calls": [{"id": "dup", "name": "rp", "args": {"run": run}}],
                    },
                ),
            )
            _add(
                con,
                1,
                run,
                base + 1,
                0,
                _msg("tool", {"content": f"res-{run}", "tool_call_id": "dup", "status": "success"}),
                tool_call_id="dup",
            )
        # run 3 未在基础 schema 中，补一行
        con.execute(
            "INSERT INTO conversation_runs VALUES (3,1,'completed','a',1,'m',NULL,'done','i','o','{}','{}','t','t',NULL)"
        )
        con.commit()
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [r["result_summary"] for r in rows] == ["res-1", "res-2", "res-3"]
        assert [r["run_id"] for r in rows] == [1, 2, 3]

    # 目的：只有调用无结果 => pending，result_sequence 为 None。潜在缺陷：pending 被误判 completed。
    def test_pending_when_no_result(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "p1", "name": "p", "args": {}}]}),
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert rows[0]["status"] == "pending"
        assert rows[0]["result_sequence"] is None

    # 目的：孤立结果（无前置调用）不产条目，避免伪造调用。潜在缺陷：orphan 被伪造成调用。
    def test_orphan_result_no_entry(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("tool", {"content": "orphan", "tool_call_id": "ghost", "status": "success"}),
            tool_call_id="ghost",
        )
        assert summarize_tool_calls(con, task_id=1, limit=50) == []

    # 目的：结果先于调用（sequence 逆序）时，结果按孤立处理、调用保持 pending。记录契约行为。
    def test_result_before_call(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            5,
            0,
            _msg("tool", {"content": "early", "tool_call_id": "lc", "status": "success"}),
            tool_call_id="lc",
        )
        _add(
            con,
            1,
            1,
            6,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "lc", "name": "lc", "args": {}}]}),
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"

    # 目的：配对必须按 (run_id, tool_call_id) 而非仅 tool_call_id —— run1 的调用没有结果、run2 复用
    # 同一 id 且有结果时，结果只能挂在 run2 的调用上，run1 必须保持 pending。
    # 潜在缺陷：只按 call_id 配对会把 run2 的结果错挂到 run1（双错：run1 假成功 + run2 假 pending）。
    def test_run_scoped_pairing_when_earlier_run_has_no_result(
        self, con: sqlite3.Connection
    ) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg(
                "ai",
                {"content": "", "tool_calls": [{"id": "dup", "name": "run1_call", "args": {}}]},
            ),
        )
        _add(
            con,
            1,
            2,
            2,
            0,
            _msg(
                "ai",
                {"content": "", "tool_calls": [{"id": "dup", "name": "run2_call", "args": {}}]},
            ),
        )
        _add(
            con,
            1,
            2,
            3,
            0,
            _msg("tool", {"content": "res-run2", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        by_name = {r["tool_name"]: r for r in rows}
        assert by_name["run1_call"]["status"] == "pending"
        assert by_name["run1_call"]["run_id"] == 1
        assert by_name["run2_call"]["status"] == "success"
        assert by_name["run2_call"]["result_summary"] == "res-run2"

    # 目的：同一 key 下存在多个未配对调用时按 FIFO 认领（结果与调用一一对应、不错位不错配）。
    # 潜在缺陷：后写覆盖 / LIFO 会让先到的调用永远 pending、结果整体错位一个。
    def test_two_pending_same_id_then_two_results_fifo(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "dup", "name": "call1", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "dup", "name": "call2", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            3,
            0,
            _msg("tool", {"content": "res-for-call1", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        _add(
            con,
            1,
            1,
            4,
            0,
            _msg("tool", {"content": "res-for-call2", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [(r["tool_name"], r["result_summary"]) for r in rows] == [
            ("call1", "res-for-call1"),
            ("call2", "res-for-call2"),
        ]

    # 目的：多个未配对调用只有部分结果时，最早的若干个被认领，其余保持 pending。
    # 潜在缺陷：结果被挂到最后创建的调用上，导致完成态调用显示 pending。
    def test_three_pending_same_id_partial_results(self, con: sqlite3.Connection) -> None:
        for seq, name in ((1, "c1"), (2, "c2"), (3, "c3")):
            _add(
                con,
                1,
                1,
                seq,
                0,
                _msg(
                    "ai", {"content": "", "tool_calls": [{"id": "dup", "name": name, "args": {}}]}
                ),
            )
        _add(
            con,
            1,
            1,
            4,
            0,
            _msg("tool", {"content": "r1", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        _add(
            con,
            1,
            1,
            5,
            0,
            _msg("tool", {"content": "r2", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [(r["tool_name"], r["status"], r["result_summary"]) for r in rows] == [
            ("c1", "success", "r1"),
            ("c2", "success", "r2"),
            ("c3", "pending", ""),
        ]

    # 目的：同一 key 下「先失败后成功」的两条结果不得互换（失败判定与结果摘要必须留在各自调用上）。
    def test_error_then_success_same_id_not_swapped(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "dup", "name": "first", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg(
                "ai", {"content": "", "tool_calls": [{"id": "dup", "name": "second", "args": {}}]}
            ),
        )
        _add(
            con,
            1,
            1,
            3,
            0,
            _msg("tool", {"content": "error: boom", "tool_call_id": "dup", "status": "error"}),
            tool_call_id="dup",
        )
        _add(
            con,
            1,
            1,
            4,
            0,
            _msg("tool", {"content": "fine", "tool_call_id": "dup", "status": "success"}),
            tool_call_id="dup",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [(r["tool_name"], r["is_error"], r["result_summary"]) for r in rows] == [
            ("first", True, "error: boom"),
            ("second", False, "fine"),
        ]

    # 目的：error 结果被识别为 is_error（status=='error' 或 content 以 'error:' 开头）。潜在缺陷：失败漏判。
    @pytest.mark.parametrize(
        "status,content,expected",
        [
            ("error", "dump", True),
            ("success", "error: bad", True),
            ("success", "ok", False),
        ],
    )
    def test_error_detection(
        self, con: sqlite3.Connection, status: str, content: str, expected: bool
    ) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "e1", "name": "e", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("tool", {"content": content, "tool_call_id": "e1", "status": status}),
            tool_call_id="e1",
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert rows[0]["is_error"] is expected

    # 目的：failures_only 只保留 is_error 条目，pending/cancelled 不算失败。潜在缺陷：pending 被当失败。
    def test_failures_only(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "ok", "name": "a", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("tool", {"content": "done", "tool_call_id": "ok", "status": "success"}),
            tool_call_id="ok",
        )
        _add(
            con,
            1,
            1,
            3,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "bad", "name": "b", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            4,
            0,
            _msg("tool", {"content": "error: x", "tool_call_id": "bad", "status": "error"}),
            tool_call_id="bad",
        )
        _add(
            con,
            1,
            1,
            5,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "pend", "name": "c", "args": {}}]}),
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50, failures_only=True)
        assert [r["tool_call_id"] for r in rows] == ["bad"]

    # 目的：limit 作用于产出条目数而非 SQL 行。潜在缺陷：limit 下推导致配对不完整。
    def test_limit_counts_entries(self, con: sqlite3.Connection) -> None:
        for i in range(5):
            _add(
                con,
                1,
                1,
                10 + i,
                0,
                _msg(
                    "ai", {"content": "", "tool_calls": [{"id": f"c{i}", "name": "t", "args": {}}]}
                ),
            )
        rows = summarize_tool_calls(con, task_id=1, limit=2)
        assert len(rows) == 2

    # 目的：contains 匹配工具名/参数/结果，忽略大小写。潜在缺陷：命中面过窄。
    def test_contains_match(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg(
                "ai",
                {
                    "content": "",
                    "tool_calls": [{"id": "s1", "name": "Search_Files", "args": {"q": "Hello"}}],
                },
            ),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("tool", {"content": "found", "tool_call_id": "s1", "status": "success"}),
            tool_call_id="s1",
        )
        assert len(summarize_tool_calls(con, task_id=1, limit=50, contains="search")) == 1
        assert len(summarize_tool_calls(con, task_id=1, limit=50, contains="hello")) == 1
        assert summarize_tool_calls(con, task_id=1, limit=50, contains="nomatch") == []

    # 目的：transport_metadata_json.status 在 tool 消息无 data.status 时兜底。潜在缺陷：状态回落错误。
    def test_transport_status_fallback(self, con: sqlite3.Connection) -> None:
        _add(
            con,
            1,
            1,
            1,
            0,
            _msg("ai", {"content": "", "tool_calls": [{"id": "cv", "name": "x", "args": {}}]}),
        )
        _add(
            con,
            1,
            1,
            2,
            0,
            _msg("tool", {"content": "", "tool_call_id": "cv"}),
            tool_call_id="cv",
            tmeta='{"status": "cancelled"}',
        )
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert rows[0]["status"] == "cancelled"


# --------------------------------------------------------------------------- #
# appdb_readonly：辅助原语边界
# --------------------------------------------------------------------------- #


class TestReadonlyHelpers:
    """LIKE 转义 / JSON 宽容解析 / require_tables 的边界。"""

    # 目的：LIKE 通配符被转义，防止用户输入扩大匹配面。潜在缺陷：%/_ 未转义。
    def test_escape_like(self) -> None:
        assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"

    # 目的：contains_pattern 两侧加通配且转义。潜在缺陷：未转义导致注入式匹配。
    def test_contains_pattern(self) -> None:
        assert contains_pattern("50%") == "%50\\%%"

    # 目的：safe_json 对 None/空串/损坏输入返回 default。潜在缺陷：解析异常外泄。
    def test_safe_json_tolerant(self) -> None:
        assert safe_json(None, default={}) == {}
        assert safe_json("", default={}) == {}
        assert safe_json("{bad", default={}) == {}
        assert safe_json('{"a":1}', default={}) == {"a": 1}

    # 目的：require_tables 缺失时错误信息含全部缺失表与库内清单。潜在缺陷：清单不全。
    def test_require_tables_message(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            con.execute("CREATE TABLE only_here (id INTEGER)")
            with pytest.raises(ValueError) as excinfo:
                require_tables(con, "a_table", "b_table")
            msg = str(excinfo.value)
            assert "a_table" in msg and "b_table" in msg
            assert "only_here" in msg
        finally:
            con.close()
