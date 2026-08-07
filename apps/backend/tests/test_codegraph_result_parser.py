"""CodeGraph 结果解析器单元测试（fixture 取自真实 Kernel 输出）。

所有正例 fixture 逐字摘自 ``docs/codegraph-tool-outputs.md``（本项目真实 Kernel
调用留档），保证测试对齐真实格式而非臆想格式。

覆盖：
- 5 个结构化工具（search/node/callers/callees/impact）的真实输出解析；
- explore 与未知工具的全文降级；
- 降级路径：空文本、格式不匹配、无 items；
- 边界：可选字段缺失、``— via edge`` 后缀有无、vendor 提示 banner 剥离、
  正文中的告警符号不被误剥离；
- 包化后的导入契约（``tool_system`` 依赖的 6 个工厂函数仍可导入）。
"""

from app.tools.tool_handler.codegraph_query.result_parser import (
    CodegraphListEntry,
    parse_call_edges,
    parse_impact,
    parse_node,
    parse_search,
    parse_tool_result,
)

# --------------------------------------------------------------------------- #
# 真实输出 fixture（逐字取自 docs/codegraph-tool-outputs.md）
# --------------------------------------------------------------------------- #

SEARCH_TEXT = """**Search Results (3 found)**

**resolve_node_binary** (function)
apps/backend/app/codegraph/node_resolver.py:48
`() -> Path`

**app.codegraph.node_resolver** (import)
apps/backend/app/codegraph/__init__.py:21
`from app.codegraph.node_resolver import resolve_node_binary`

**app.codegraph.node_resolver** (import)
apps/backend/app/codegraph/supervisor.py:29
`from app.codegraph.node_resolver import resolve_node_binary`
"""

NODE_TEXT = """**resolve_node_binary** (function)

**Location:** apps/backend/app/codegraph/node_resolver.py:48
**Signature:** `() -> Path`
**Trail — codegraph_node any of these to follow it (no Read needed)**
**Calls →** get (apps/backend/app/storage/crud/task_crud.py:139), resolve \
(apps/backend/app/core/agents/agent_profile_registry.py:43), _fixed_node_candidates \
(apps/backend/app/codegraph/node_resolver.py:26), CodeGraphNodeMissingError \
(apps/backend/app/codegraph/exceptions.py:102), NODE_DIR_ENV \
(apps/backend/app/codegraph/node_resolver.py:23)
**Called by ←** _spawn (apps/backend/app/codegraph/supervisor.py:238), __init__.py \
(apps/backend/app/codegraph/__init__.py:1), supervisor.py \
(apps/backend/app/codegraph/supervisor.py:1)
"""

CALLERS_TEXT = """**Callers of resolve_node_binary (3 found)**

- _spawn (method) - apps/backend/app/codegraph/supervisor.py:238
- __init__.py (file) - apps/backend/app/codegraph/__init__.py:1 — via import
- supervisor.py (file) - apps/backend/app/codegraph/supervisor.py:1 — via import
"""

CALLEES_TEXT = """**Callees of resolve_node_binary (5 found)**

- get (method) - apps/backend/app/storage/crud/task_crud.py:139
- resolve (method) - apps/backend/app/core/agents/agent_profile_registry.py:43
- _fixed_node_candidates (function) - apps/backend/app/codegraph/node_resolver.py:26
- CodeGraphNodeMissingError (class) - apps/backend/app/codegraph/exceptions.py:102 \
— via instantiation
- NODE_DIR_ENV (variable) - apps/backend/app/codegraph/node_resolver.py:23 — via reference
"""

IMPACT_TEXT = """**Impact: "resolve_node_binary" affects 9 symbols**

**apps/backend/app/codegraph/node_resolver.py:**
resolve_node_binary:48

**apps/backend/app/codegraph/supervisor.py:**
_spawn:238, start:118, supervisor.py:1

**apps/backend/app/codegraph/__init__.py:**
__init__.py:1
"""

EXPLORE_TEXT = """**Exploration: CodegraphQueryTool 如何把 6 个工具分发到 Kernel**

Found 43 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `query` (apps/backend/app/storage/crud/log_crud.py:79) — 1 caller; \
⚠️ no covering tests found
"""


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def test_parse_search_real_output():
    """真实 search 输出应解析出 3 条，含 kind/路径/行号/签名。"""
    result = parse_tool_result("codegraph_search", SEARCH_TEXT)
    items = result["items"]
    assert result["tool"] == "codegraph_search"
    assert len(items) == 3
    assert items[0] == {
        "name": "resolve_node_binary",
        "kind": "function",
        "filePath": "apps/backend/app/codegraph/node_resolver.py",
        "lineNumber": 48,
        "edge": "",
        "signature": "() -> Path",
    }
    # 同名不同位置的 import 条目不应被去重合并。
    assert items[1]["filePath"] == "apps/backend/app/codegraph/__init__.py"
    assert items[2]["filePath"] == "apps/backend/app/codegraph/supervisor.py"
    assert items[1]["kind"] == "import"


def test_parse_search_without_signature_line():
    """签名行缺失时不丢条目，signature 留空。"""
    entries = parse_search(["**foo** (class)", "a/b.py:10", "", "**bar** (function)", "c.py:2"])
    assert [e.name for e in entries] == ["foo", "bar"]
    assert entries[0].signature == ""
    assert entries[1].filePath == "c.py"
    assert entries[1].lineNumber == 2


def test_parse_search_heading_without_location_is_skipped():
    """裸标题（无紧跟的位置行）不产出条目：它只是签名正文里的同形文本。"""
    assert parse_search(["**solo** (variable)"]) == []


def test_parse_search_multiline_signature_does_not_leak_fake_entries():
    """多行签名内部与标题同形的行不得被误判为新符号（真实 Kernel 回归场景）。

    索引到「内容本身就是 CodeGraph 输出样例」的测试 fixture 时，vendor 会原样回显
    跨行字符串，签名内部出现 ``**resolve_node_binary** (function)`` 等同形行。
    正确行为是整段签名被跳过，只产出真实的两个符号。
    """
    lines = [
        "**SEARCH_TEXT** (variable)",
        "apps/backend/tests/test_codegraph_result_parser.py:28",
        '`= """**Search Results (3 found)**',
        "",
        "**resolve_node_binary** (function)",
        "apps/backend/app/codegraph/node_...`",
        "",
        "**real_symbol** (function)",
        "apps/backend/app/real.py:5",
        "`() -> None`",
    ]
    entries = parse_search(lines)

    assert [e.name for e in entries] == ["SEARCH_TEXT", "real_symbol"]
    assert all(e.filePath for e in entries), "不得产出无路径的脏条目"
    assert entries[0].signature.endswith("…")
    assert entries[1].signature == "() -> None"


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #


def test_parse_node_real_output():
    """node 输出应产出「符号自身 + 5 calls + 3 called by」共 9 条。"""
    result = parse_tool_result("codegraph_node", NODE_TEXT)
    items = result["items"]
    assert len(items) == 9

    head = items[0]
    assert head["name"] == "resolve_node_binary"
    assert head["kind"] == "function"
    assert head["filePath"] == "apps/backend/app/codegraph/node_resolver.py"
    assert head["lineNumber"] == 48
    assert head["signature"] == "() -> Path"
    assert head["edge"] == ""

    calls = [i for i in items if i["edge"] == "calls"]
    called_by = [i for i in items if i["edge"] == "called by"]
    assert len(calls) == 5
    assert len(called_by) == 3
    # split("), ") 补回右括号的逻辑：首项与末项都必须正确。
    assert calls[0] == {
        "name": "get",
        "kind": "",
        "filePath": "apps/backend/app/storage/crud/task_crud.py",
        "lineNumber": 139,
        "edge": "calls",
        "signature": "",
    }
    assert calls[-1]["name"] == "NODE_DIR_ENV"
    assert calls[-1]["lineNumber"] == 23
    assert called_by[-1]["name"] == "supervisor.py"
    assert called_by[-1]["lineNumber"] == 1


def test_parse_node_trail_only():
    """没有符号标题、只有 trail 时仍产出 trail 条目。"""
    entries = parse_node(["**Calls →** a (x/y.py:3)"])
    assert entries == [CodegraphListEntry(name="a", filePath="x/y.py", lineNumber=3, edge="calls")]


# --------------------------------------------------------------------------- #
# callers / callees（同构，共用 parser）
# --------------------------------------------------------------------------- #


def test_parse_callers_real_output():
    """callers 输出 3 条，其中 2 条带 ``— via import`` 边标签。"""
    result = parse_tool_result("codegraph_callers", CALLERS_TEXT)
    items = result["items"]
    assert len(items) == 3
    assert items[0] == {
        "name": "_spawn",
        "kind": "method",
        "filePath": "apps/backend/app/codegraph/supervisor.py",
        "lineNumber": 238,
        "edge": "",
        "signature": "",
    }
    assert items[1]["edge"] == "import"
    assert items[2]["edge"] == "import"


def test_parse_callees_real_output():
    """callees 输出 5 条，边标签含多词 ``instantiation`` / ``reference``。"""
    result = parse_tool_result("codegraph_callees", CALLEES_TEXT)
    items = result["items"]
    assert len(items) == 5
    assert [i["kind"] for i in items] == [
        "method",
        "method",
        "function",
        "class",
        "variable",
    ]
    assert items[3]["edge"] == "instantiation"
    assert items[4]["edge"] == "reference"
    assert items[0]["edge"] == ""


def test_parse_call_edges_ignores_non_matching_lines():
    """标题行、空行等非条目行被忽略，不产生噪音条目。"""
    entries = parse_call_edges(
        [
            "**Callers of x (1 found)**",
            "",
            "- a (method) - p.py:1",
            "random noise line",
        ]
    )
    assert entries == [CodegraphListEntry(name="a", kind="method", filePath="p.py", lineNumber=1)]


# --------------------------------------------------------------------------- #
# impact
# --------------------------------------------------------------------------- #


def test_parse_impact_real_output():
    """impact 按文件分节扁平化：3 个分节共 5 个符号，各自带所属文件路径。"""
    result = parse_tool_result("codegraph_impact", IMPACT_TEXT)
    items = result["items"]
    assert len(items) == 5
    assert items[0] == {
        "name": "resolve_node_binary",
        "kind": "",
        "filePath": "apps/backend/app/codegraph/node_resolver.py",
        "lineNumber": 48,
        "edge": "",
        "signature": "",
    }
    # 同一行逗号分隔的 3 个符号都归属同一文件。
    supervisor_items = [
        i for i in items if i["filePath"] == "apps/backend/app/codegraph/supervisor.py"
    ]
    assert [i["name"] for i in supervisor_items] == ["_spawn", "start", "supervisor.py"]
    assert [i["lineNumber"] for i in supervisor_items] == [238, 118, 1]
    assert items[-1]["filePath"] == "apps/backend/app/codegraph/__init__.py"


def test_parse_impact_ignores_symbols_before_any_file_heading():
    """文件分节出现前的裸行不产出条目（避免把标题当符号）。"""
    entries = parse_impact(['**Impact: "x" affects 1 symbols**', "stray:1"])
    assert entries == []


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #


def test_explore_falls_back_to_raw():
    """explore 本期不结构化，恒返回全文，且不带 items。"""
    result = parse_tool_result("codegraph_explore", EXPLORE_TEXT)
    assert "items" not in result
    assert result["tool"] == "codegraph_explore"
    assert result["raw"].startswith("**Exploration:")


def test_explore_inline_warning_symbol_not_stripped_as_notice():
    """正文中的 ⚠️（blast radius 内容）不应被当 banner 剥离。"""
    result = parse_tool_result("codegraph_explore", EXPLORE_TEXT)
    assert "notice" not in result
    assert "⚠️ no covering tests found" in result["raw"]


def test_unknown_tool_falls_back_to_raw():
    """未知工具名走全文降级，不抛异常。"""
    result = parse_tool_result("codegraph_future_tool", "whatever")
    assert result == {"tool": "codegraph_future_tool", "raw": "whatever"}


def test_unparsable_text_falls_back_to_raw():
    """结构化工具遇到无法解析的文本时降级全文，不丢数据。"""
    result = parse_tool_result("codegraph_search", "No results from CodeGraph query.")
    assert "items" not in result
    assert result["raw"] == "No results from CodeGraph query."


def test_empty_text_falls_back_to_raw():
    """空文本降级为空 raw，不抛异常。"""
    result = parse_tool_result("codegraph_callers", "")
    assert result == {"tool": "codegraph_callers", "raw": ""}


def test_leading_notice_banner_is_split_out():
    """vendor 前置提示 banner 被剥离到 notice，正文仍正常解析。"""
    text = "⚠️ Index is stale; results may be outdated.\n\n" + CALLERS_TEXT
    result = parse_tool_result("codegraph_callers", text)
    assert result["notice"] == "⚠️ Index is stale; results may be outdated."
    assert len(result["items"]) == 3
    assert result["items"][0]["name"] == "_spawn"


# --------------------------------------------------------------------------- #
# 包化后的导入契约
# --------------------------------------------------------------------------- #


def test_package_reexports_all_factories():
    """包化后 6 个工厂函数与 handler 类仍可从包名直接导入（tool_system 依赖）。"""
    from app.tools.tool_handler import codegraph_query

    for factory_name in (
        "build_codegraph_explore_definition",
        "build_codegraph_search_definition",
        "build_codegraph_node_definition",
        "build_codegraph_callers_definition",
        "build_codegraph_callees_definition",
        "build_codegraph_impact_definition",
    ):
        definition = getattr(codegraph_query, factory_name)()
        assert definition.display.expand_layout == "list"
        assert definition.display.icon == "network"
    assert codegraph_query.CodegraphQueryTool is not None
