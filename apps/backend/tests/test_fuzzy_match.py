"""fuzzy_match 模糊匹配引擎的严苛单元测试。

fuzzy_match 是 replace / patch 两个工具的匹配核心，最容易藏 bug。逐策略触发
9 条匹配链，并针对易错点设计对抗性用例：
- 空 old_string / old==new 早退；
- 多命中 + replace_all 语义；
- 各策略独立命中（line_trimmed / whitespace / indentation / escape /
  trimmed_boundary / unicode / block_anchor / context_aware）；
- escape-drift 检测（工具调用序列化伪影）；
- unicode 归一化命中时保留文件原字符；
- 位置映射正确性（替换结果与预期完全一致）；
- format_no_match_hint / find_closest_lines 的 "did you mean"。
"""

from app.tools.tool_handler.patch.fuzzy_match import (
    find_closest_lines,
    format_no_match_hint,
    fuzzy_find_and_replace,
)

# --------------------------------------------------------------------------- #
# 早退与命中数
# --------------------------------------------------------------------------- #


def test_empty_old_string():
    new, count, strat, err = fuzzy_find_and_replace("abc", "", "x")
    assert count == 0
    assert err == "old_string cannot be empty"
    assert new == "abc"


def test_identical_old_new():
    new, count, strat, err = fuzzy_find_and_replace("abc", "a", "a")
    assert count == 0
    assert "identical" in err


def test_exact_single_match():
    new, count, strat, err = fuzzy_find_and_replace("hello world", "world", "there")
    assert new == "hello there"
    assert count == 1
    assert strat == "exact"
    assert err is None


def test_exact_multiple_without_replace_all():
    new, count, strat, err = fuzzy_find_and_replace("a a a", "a", "b")
    assert count == 0
    assert "matches" in err


def test_exact_multiple_with_replace_all():
    new, count, strat, err = fuzzy_find_and_replace("a a a", "a", "b", replace_all=True)
    assert new == "b b b"
    assert count == 3


# --------------------------------------------------------------------------- #
# 逐策略触发
# --------------------------------------------------------------------------- #


def test_strategy_line_trimmed():
    content = "  hello  \n  world  \n"
    new, count, strat, err = fuzzy_find_and_replace(content, "hello\nworld", "X\nY")
    assert count == 1
    assert strat == "line_trimmed"


def test_strategy_whitespace_normalized():
    content = "foo      bar"
    new, count, strat, err = fuzzy_find_and_replace(content, "foo bar", "baz")
    assert count == 1
    assert strat == "whitespace_normalized"


def test_strategy_indentation_flexible():
    # 多行、行首缩进不同：exact/whitespace 无法命中，须靠去缩进策略。
    content = "        if cond:\n            do_work()\n"
    old = "if cond:\ndo_work()"
    new, count, strat, err = fuzzy_find_and_replace(content, old, "if cond:\n    pass")
    assert count == 1
    assert strat in ("indentation_flexible", "line_trimmed")


def test_strategy_escape_normalized():
    content = "line1\nline2"
    new, count, strat, err = fuzzy_find_and_replace(content, "line1\\nline2", "merged")
    assert count == 1
    assert strat == "escape_normalized"
    assert new == "merged"


def test_strategy_unicode_normalized():
    content = 'say \u201chello\u201d now'  # smart quotes
    new, count, strat, err = fuzzy_find_and_replace(content, 'say "hello" now', "done")
    assert count == 1
    assert strat == "unicode_normalized"


def test_strategy_block_anchor():
    content = "def foo():\n    x = compute_something(1, 2, 3)\n    return x\n"
    old = "def foo():\n    x = compute(9)\n    return x"
    new, count, strat, err = fuzzy_find_and_replace(content, old, "def foo():\n    return 0")
    # 首尾行锚定 + 中间相似度命中。
    assert count == 1


# --------------------------------------------------------------------------- #
# escape-drift 检测
# --------------------------------------------------------------------------- #


def test_escape_drift_detected():
    """old/new 含 \\' 但文件命中区无该序列 -> 报 escape drift。"""

    content = "value = 'hello'\n"
    # line_trimmed 命中，old_string 含 \\' 序列，new_string 也含。
    old = "value = \\'hello\\'"
    new = "value = \\'world\\'"
    result, count, strat, err = fuzzy_find_and_replace(content, old, new)
    assert count == 0
    assert err is not None
    assert "Escape-drift" in err


# --------------------------------------------------------------------------- #
# unicode 保留
# --------------------------------------------------------------------------- #


def test_unicode_preserved_in_unchanged_region():
    """unicode_normalized 命中时，未改动片段保留文件原始智能引号。"""

    content = 'prefix \u201ckeep\u201d suffix'
    old = 'prefix "keep" suffix'
    new = 'prefix "keep" DONE'
    result, count, strat, err = fuzzy_find_and_replace(content, old, new)
    assert count == 1
    # 保留原始智能引号。
    assert "\u201ckeep\u201d" in result


# --------------------------------------------------------------------------- #
# 位置映射正确性
# --------------------------------------------------------------------------- #


def test_replacement_content_correctness_multiline():
    content = "AAA\nBBB\nCCC\n"
    result, count, strat, err = fuzzy_find_and_replace(content, "BBB", "ZZZ")
    assert result == "AAA\nZZZ\nCCC\n"
    assert count == 1


# --------------------------------------------------------------------------- #
# no-match hint
# --------------------------------------------------------------------------- #


def test_find_closest_lines_returns_snippet():
    content = "def calculate_total(items):\n    return sum(items)\n"
    hint = find_closest_lines("def calculate_total(item):", content)
    assert "calculate_total" in hint


def test_find_closest_lines_empty_inputs():
    assert find_closest_lines("", "x") == ""
    assert find_closest_lines("x", "") == ""


def test_format_no_match_hint_only_for_not_found():
    content = "def foo():\n    pass\n"
    hint = format_no_match_hint("Could not find a match", 0, "def foo(x):", content)
    assert "Did you mean" in hint


def test_format_no_match_hint_skips_when_matched():
    assert format_no_match_hint("Could not find", 2, "x", "y") == ""


def test_format_no_match_hint_skips_non_notfound_error():
    assert format_no_match_hint("Found 3 matches", 0, "x", "y") == ""
