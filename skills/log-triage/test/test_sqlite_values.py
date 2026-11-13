#!/usr/bin/env python3
"""log-triage 查询脚本的独立单元测试。

只测行为契约：布尔归一（Python/SQL 双侧一致性）、消息回放 ``--in-context-only`` 过滤口径、
stuck 未收敛体检、工具调用配对、缺表可诊断性与参数边界。

业务库用后端 ORM 建表（见 ``app_db_fixture``）；造数的消息字面量天然超长，故对本文件豁免行宽。
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
        expr = bool_true_sql("c.include_in_context")
        for item in BOOL_FALSE_TEXTS:
            assert f"'{item}'" in expr


# --------------------------------------------------------------------------- #
# 临时库：表结构来自后端 ORM
# --------------------------------------------------------------------------- #


@pytest.fixture()
def con(tmp_path: Path) -> sqlite3.Connection:
    """task 1 下两个 run：run 1 completed，run 2 running。"""

    connection = create_app_db(tmp_path / "app.sqlite3")
    add_workspace(connection, name="ws1", root_path="/tmp/ws1")
    add_task(connection, title="t1")
    add_run(connection, task_id=1, status="completed")
    add_run(connection, task_id=1, status="running")
    yield connection
    connection.close()


def _add(
    con: sqlite3.Connection,
    seq: int,
    message: dict | str,
    metadata: dict | None = None,
    *,
    run: int | None = 1,
    in_context: object = 1,
    turn: int = 1,
) -> None:
    add_context(
        con,
        task_id=1,
        run_id=run,
        sequence=seq,
        message=message,
        metadata=metadata,
        turn=turn,
        include_in_context=in_context,
    )


# --------------------------------------------------------------------------- #
# list_messages：--in-context-only 过滤与显示自洽性
# --------------------------------------------------------------------------- #


class TestListMessagesInContextFilter:
    """messages 显示的 include_in_context 与 --in-context-only 过滤口径必须自洽。"""

    # 目的：TEXT 假值（'false'/'no'/'FALSE'/'0'/''）必须被 --in-context-only 排除且显示为 False。
    # 潜在缺陷：裸比较把 TEXT 'false' 当真，流式草稿混进「模型看到的上下文」。
    @pytest.mark.parametrize("stored", ["false", "no", "FALSE", "0", "", 0])
    def test_falsey_is_excluded(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, assistant_message("draft"), in_context=stored)
        assert list_messages(con, task_id=1, limit=50, in_context_only=True) == []
        shown = list_messages(con, task_id=1, limit=50)
        assert shown[0]["include_in_context"] is False

    # 目的：真值（'true'/'1'/1）保留且显示为 True。潜在缺陷：TEXT 'true' 被误删。
    @pytest.mark.parametrize("stored", ["true", "TRUE", "1", 1])
    def test_truthy_is_kept(self, con: sqlite3.Connection, stored: object) -> None:
        _add(con, 1, user_message("keep-me"), {"kind": "user_input"}, in_context=stored)
        rows = list_messages(con, task_id=1, limit=50, in_context_only=True)
        assert len(rows) == 1
        assert rows[0]["include_in_context"] is True
        assert rows[0]["kind"] == "user_input"

    # 目的：同一行在「显示」与「过滤」两侧自洽。潜在缺陷：两侧口径分叉。
    @pytest.mark.parametrize("stored", ["false", "no", "0", "", "true", "1", 1, 0, " 1 "])
    def test_display_and_filter_self_consistent(
        self, con: sqlite3.Connection, stored: object
    ) -> None:
        _add(con, 1, user_message("x"), in_context=stored)
        shown = list_messages(con, task_id=1, limit=50)
        filtered = list_messages(con, task_id=1, limit=50, in_context_only=True)
        assert len(filtered) == (1 if shown[0]["include_in_context"] else 0)

    # 目的：非法 order 抛 ValueError。潜在缺陷：静默接受导致 SQL 拼接。
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

    # 目的：desc + limit 与 in_context_only 组合时 LIMIT 在过滤之后生效。潜在缺陷：过滤前 LIMIT 挤掉有效行。
    def test_desc_order_filter_before_limit(self, con: sqlite3.Connection) -> None:
        _add(con, 1, user_message("real"))
        for seq in range(2, 7):
            _add(con, seq, assistant_message(f"draft{seq}"), in_context=0)
        rows = list_messages(con, task_id=1, limit=5, order="desc", in_context_only=True)
        assert [r["text"] for r in rows] == ["real"]

    # 目的：run_id 过滤；不带 run_id 时含继承来的 run_id 为空的行。潜在缺陷：run 过滤失效或继承行被丢。
    def test_run_id_scoping(self, con: sqlite3.Connection) -> None:
        _add(con, 1, user_message("inherited"), run=None)
        _add(con, 2, user_message("r1"), run=1)
        _add(con, 3, user_message("r2"), run=2)
        assert [r["text"] for r in list_messages(con, task_id=1, run_id=2, limit=10)] == ["r2"]
        assert len(list_messages(con, task_id=1, limit=10)) == 3

    # 目的：多段 content 只拼文本段，assistant 行带 finish_reason 与 usage，tool 行带卡片状态与错误。
    # 潜在缺陷：多模态消息正文丢失，或角色专属 metadata 串到别的角色。
    def test_describe_fields_by_role(self, con: sqlite3.Connection) -> None:
        _add(con, 1, {"role": "user", "content": [{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "x"}}]}, {"kind": "user_input"})
        _add(con, 2, assistant_message("", ("c1", "read_file", "{not json")), {"finish_reason": "tool_calls", "usage": {"total_tokens": 3}})
        _add(con, 3, tool_message("c1", "boom"), {"status": "failed", "error": "文件不存在"})
        user, assistant, tool = list_messages(con, task_id=1, limit=10)
        assert user["text"] == "看图"
        assert (assistant["kind"], assistant["usage"]) == ("tool_calls", {"total_tokens": 3})
        assert assistant["tool_calls"] == [{"id": "c1", "name": "read_file", "args": "{not json"}]
        assert (tool["tool_call_id"], tool["tool_status"], tool["error"]) == ("c1", "failed", "文件不存在")


# --------------------------------------------------------------------------- #
# context_statistics：计数口径
# --------------------------------------------------------------------------- #


class TestContextStatistics:
    """context_statistics 的 in_context / runs / turns / 分组口径。"""

    # 目的：TEXT 形态的布尔按 as_bool 计数，excluded = total - in_context。潜在缺陷：TEXT 'false' 被计入上下文。
    def test_in_context_counts_text_bools(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("d"), in_context="false")
        _add(con, 2, assistant_message("d"), in_context=" 1 ")
        _add(con, 3, user_message("h"), {"kind": "user_input"}, in_context="true")
        stats = context_statistics(con, task_id=1)
        assert (stats["total"], stats["in_context"], stats["excluded"]) == (3, 2, 1)

    # 目的：runs / turns 去重计数，继承行（run_id 为空）不算 run；user 行按 kind 分组。
    def test_runs_turns_and_kinds(self, con: sqlite3.Connection) -> None:
        _add(con, 1, user_message("old"), {"kind": "user_input"}, run=None, turn=1)
        _add(con, 2, user_message("go"), {"kind": "user_input"}, run=1, turn=2)
        _add(con, 3, user_message("<system_notice>"), {"kind": "notice"}, run=2, turn=2)
        _add(con, 4, user_message("from agent"), {"kind": "agent_message"}, run=2, turn=3)
        _add(con, 5, assistant_message("ok"), run=2, turn=3)
        stats = context_statistics(con, task_id=1)
        assert (stats["runs"], stats["turns"]) == (2, 3)
        assert stats["by_role"] == {"user": 4, "assistant": 1}
        assert stats["by_user_kind"] == {"user_input": 2, "notice": 1, "agent_message": 1}

    # 目的：损坏 message_json 归为 unknown 且不抛。潜在缺陷：JSON 解析失败导致异常。
    def test_corrupt_json_is_unknown_role(self, con: sqlite3.Connection) -> None:
        _add(con, 1, "{not json")
        assert context_statistics(con, task_id=1)["by_role"] == {"unknown": 1}


# --------------------------------------------------------------------------- #
# find_unsettled：未收敛体检
# --------------------------------------------------------------------------- #


class TestFindUnsettled:
    """stuck 的活动 run、未结算工具占位与排队积压。"""

    # 目的：pending / running 的工具结果行报为未结算占位。潜在缺陷：HITL 等待或重放中的调用漏报。
    @pytest.mark.parametrize("status", ["pending", "running"])
    def test_open_placeholder_reported(self, con: sqlite3.Connection, status: str) -> None:
        _add(con, 1, assistant_message("", ("c1", "t", {})))
        _add(con, 2, tool_message("c1", "waiting"), {"status": status})
        [row] = find_unsettled(con, limit=50)["open_tool_placeholders"]
        assert (row["tool_call_id"], row["tool_status"], row["run_status"]) == ("c1", status, "completed")
        assert row["is_background"] == 0

    # 目的：已结算的结果行不报。潜在缺陷：把终态卡片当未结算，体检噪声淹没真问题。
    @pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
    def test_settled_not_reported(self, con: sqlite3.Connection, status: str) -> None:
        _add(con, 1, tool_message("c1", "done"), {"status": status})
        assert find_unsettled(con, limit=50)["open_tool_placeholders"] == []

    # 目的：带 background 的占位标为后台；user 行即使 metadata 写了 status 也不算工具占位。
    # 潜在缺陷：后台命令与 HITL 等待分不开，或按 metadata 误判非 tool 行。
    def test_background_flag_and_role_filter(self, con: sqlite3.Connection) -> None:
        _add(con, 1, tool_message("bg", "running"), {"status": "pending", "background": {"label": "build"}})
        _add(con, 2, user_message("x"), {"kind": "user_input", "status": "pending"})
        [row] = find_unsettled(con, limit=50)["open_tool_placeholders"]
        assert (row["tool_call_id"], row["is_background"]) == ("bg", 1)

    # 目的：排队消息按 Task 汇总条数与种类。潜在缺陷：「发了没反应」时看不到外部队列积压。
    def test_queued_messages_grouped_by_task(self, con: sqlite3.Connection) -> None:
        add_task(con, title="t2")
        add_task_message(con, task_id=1, kind="user_input")
        add_task_message(con, task_id=1, kind="background_result", delivery="immediate")
        add_task_message(con, task_id=2, kind="system_notice")
        queued = {q["task_id"]: q for q in find_unsettled(con, limit=50)["queued_messages"]}
        assert queued[1]["queued"] == 2
        assert set(queued[1]["kinds"].split(",")) == {"user_input", "background_result"}
        assert (queued[2]["title"], queued[2]["queued"]) == ("t2", 1)

    # 目的：活动 run（running/suspended）列出、终态不列；current_run_id 指向活动 run 的 task 列出。
    def test_active_runs_listed(self, con: sqlite3.Connection) -> None:
        add_run(con, task_id=1, status="suspended")
        add_run(con, task_id=1, status="cancelled")
        con.execute("UPDATE tasks SET current_run_id = 3 WHERE id = 1")
        con.commit()
        result = find_unsettled(con, limit=50)
        assert {r["id"] for r in result["active_runs"]} == {2, 3}
        assert [(t["id"], t["current_run_status"]) for t in result["tasks_with_active_run"]] == [(1, "suspended")]

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
# summarize_tool_calls：配对
# --------------------------------------------------------------------------- #


class TestToolCallPairing:
    """工具调用与结果按 tool_call_id 配对（id 在 Task 内唯一）。"""

    # 目的：无 id / 空 id 的调用不产条目，同一消息里有 id 的调用照常产出。潜在缺陷：无 id 调用被渲染成永久缺结果。
    def test_idless_and_empty_id_skipped(self, con: sqlite3.Connection) -> None:
        message = assistant_message("", ("", "e", {}), ("c1", "ok", {}))
        message["tool_calls"].insert(0, {"type": "function", "function": {"name": "no_id", "arguments": "{}"}})
        _add(con, 1, message)
        _add(con, 2, tool_message("c1", "r"), {"status": "completed"})
        rows = summarize_tool_calls(con, task_id=1, limit=50)
        assert [(r["tool_call_id"], r["tool_name"], r["status"]) for r in rows] == [("c1", "ok", "completed")]

    # 目的：只有调用无结果 => no_result，result_sequence 为 None；与 pending 占位区分。潜在缺陷：缺结果被说成在等待。
    def test_no_result_when_missing(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("", ("p1", "p", {"x": 1})), turn=3)
        [row] = summarize_tool_calls(con, task_id=1, limit=50)
        assert (row["status"], row["result_sequence"], row["background"], row["error"]) == ("no_result", None, None, "")
        assert (row["args"], row["run_id"], row["turn"], row["request_sequence"]) == ({"x": 1}, 1, 3, 1)

    # 目的：状态与后台记录取结果行 metadata；metadata 无 status 时视为 completed。潜在缺陷：状态来源错。
    def test_status_from_result_metadata(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("", ("a", "x", {}), ("b", "y", {}), ("c", "z", {})))
        _add(con, 2, tool_message("a", "bg"), {"status": "pending", "background": {"label": "build"}})
        _add(con, 3, tool_message("b", ""), {"status": "cancelled"})
        _add(con, 4, tool_message("c", "plain"))
        rows = {r["tool_call_id"]: r for r in summarize_tool_calls(con, task_id=1, limit=50)}
        assert (rows["a"]["status"], rows["a"]["background"]) == ("pending", {"label": "build"})
        assert rows["b"]["status"] == "cancelled"
        assert (rows["c"]["status"], rows["c"]["result_sequence"]) == ("completed", 4)

    # 目的：孤立结果（无前置调用）不产条目；结果先于调用时调用保持 no_result。潜在缺陷：伪造调用或错配。
    def test_orphan_and_result_before_call(self, con: sqlite3.Connection) -> None:
        _add(con, 1, tool_message("ghost", "orphan"), {"status": "completed"})
        _add(con, 2, tool_message("lc", "early"), {"status": "completed"})
        _add(con, 3, assistant_message("", ("lc", "lc", {})))
        [row] = summarize_tool_calls(con, task_id=1, limit=50)
        assert (row["tool_call_id"], row["status"]) == ("lc", "no_result")

    # 目的：run_id 只筛发起调用的 assistant 行，结果行可在别的 run 并入（后台结果在新 Run 回投）。
    # 潜在缺陷：按结果行 run 筛选，跨 Run 回投的结果丢失。
    def test_run_filter_by_request_row(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("", ("r1", "first", {})), run=1)
        _add(con, 2, assistant_message("", ("r2", "second", {})), run=2)
        _add(con, 3, tool_message("r1", "late"), {"status": "completed"}, run=2)
        [row] = summarize_tool_calls(con, task_id=1, run_id=1, limit=50)
        assert (row["tool_call_id"], row["status"], row["result_summary"]) == ("r1", "completed", "late")

    # 目的：failed 带 error 文本；failures_only 只留 failed，cancelled / pending / no_result 不算失败。
    def test_failures_only(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("", ("ok", "a", {}), ("bad", "b", {}), ("cx", "c", {}), ("hold", "d", {}), ("lost", "e", {})))
        _add(con, 2, tool_message("ok", "done"), {"status": "completed"})
        _add(con, 3, tool_message("bad", "error: x"), {"status": "failed", "error": "权限不足"})
        _add(con, 4, tool_message("cx", ""), {"status": "cancelled"})
        _add(con, 5, tool_message("hold", ""), {"status": "pending"})
        rows = summarize_tool_calls(con, task_id=1, limit=50, failures_only=True)
        assert [(r["tool_call_id"], r["error"]) for r in rows] == [("bad", "权限不足")]

    # 目的：limit 作用于产出条目数而非 SQL 行。潜在缺陷：limit 下推导致配对不完整。
    def test_limit_counts_entries(self, con: sqlite3.Connection) -> None:
        for i in range(5):
            _add(con, 10 + i, assistant_message("", (f"c{i}", "t", {})))
        assert len(summarize_tool_calls(con, task_id=1, limit=2)) == 2

    # 目的：contains 匹配工具名/参数/结果，忽略大小写；非法 JSON 参数原样保留。潜在缺陷：命中面过窄或解析异常。
    def test_contains_match(self, con: sqlite3.Connection) -> None:
        _add(con, 1, assistant_message("", ("s1", "Search_Content", {"q": "Hello"}), ("s2", "raw", "{oops")))
        _add(con, 2, tool_message("s1", "found"), {"status": "completed"})
        assert len(summarize_tool_calls(con, task_id=1, limit=50, contains="search")) == 1
        assert len(summarize_tool_calls(con, task_id=1, limit=50, contains="hello")) == 1
        assert len(summarize_tool_calls(con, task_id=1, limit=50, contains="FOUND")) == 1
        assert summarize_tool_calls(con, task_id=1, limit=50, contains="nomatch") == []
        [raw] = summarize_tool_calls(con, task_id=1, limit=50, contains="raw")
        assert raw["args"] == "{oops"


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
