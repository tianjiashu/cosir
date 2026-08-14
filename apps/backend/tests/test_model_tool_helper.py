"""``ModelToolHelper`` 纯决策层单元测试（invalid_tool_calls 自愈 Task 3 测试点 1-3）。

只覆盖 ``model_tool_helper`` 的三个纯函数：工具名提取、双轨决策、修复提示构造。
不涉及节点执行、事件与 graph 路由（那些在 model_node / edges / graph 级测试里）。
"""

from __future__ import annotations

from typing import Any

from app.core.workflows.nodes.model_tool_helper import InvalidToolOutcome, ModelToolHelper


def _repair_data(tool_name: str, **invalid: Any) -> dict[str, Any]:
    """构造一条 ``build_invalid_tool_call_repair_message`` 入参明细。

    参数:
        tool_name: 命中的工具名。
        **invalid: 写入 ``invalid_tool_call`` 的字段（如 ``args`` / ``error``）。

    返回:
        形如 ``{"tool_name": ..., "invalid_tool_call": {...}}`` 的明细字典。

    异常:
        无。

    副作用:
        无。
    """

    return {"tool_name": tool_name, "invalid_tool_call": dict(invalid)}


# =============================================================================
# 测试点 1：invalid_tool_call_mention_tool_name
# =============================================================================


def test_invalid_tool_call_mention_tool_name() -> None:
    """测试目的：覆盖 name 提取的四类契约——精确 name 命中、词边界兜底、
    ``read_file_x`` 不误命中 ``read_file``、完全未命中返回 None、available 空返回 None。

    可能发现的缺陷（plan §0.1 #10 / #5）：
    - 脆弱的 ``tool_name in str(...)`` 子串匹配把 ``read_file_x`` 误判为 ``read_file``；
    - 精确 name 分支缺失导致随机命中 available 集合里其他工具名；
    - available 空集合下仍返回非 None 假命中。
    """

    # 1) 精确 name 命中
    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": "read_file", "args": "{", "error": "bad json"},
            {"read_file", "search_files"},
        )
        == "read_file"
    )

    # 2) 词边界兜底：name 缺失但 args 文本出现 ``read_file(...)`` 独立 token
    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": None, "args": 'call read_file(path="a.py")', "error": "parse error"},
            {"read_file", "search_files"},
        )
        == "read_file"
    )

    # 3) 词边界缺失（read_file_x 是其他 token 的子串）不得误命中
    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": None, "args": "read_file_x(1)"},
            {"read_file"},
        )
        is None
    )

    # 4) 完全无工具名线索的残片返回 None
    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": "", "args": '"', "error": "Unterminated string"},
            {"read_file", "search_files"},
        )
        is None
    )

    # 5) available 空集合恒返回 None（不可能存在真实调用意图）
    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": "read_file", "args": "{"},
            set(),
        )
        is None
    )


def test_mention_tool_name_exact_name_wins_over_other_mentioned_tool() -> None:
    """测试目的：精确 ``name`` 优先级高于 args 文本里提到的其它工具名。

    可能发现的缺陷：实现先做词边界扫描再看 name，导致返回 args 里偶然出现的
    无关工具名，修复提示指向错误工具。
    """

    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": "read_file", "args": '{"cmd": "search_files"'},
            {"read_file", "search_files"},
        )
        == "read_file"
    )


def test_mention_tool_name_unrelated_prompt_text_does_not_hit() -> None:
    """测试目的：无关中文 prompt 片段（"请读取并搜索文件"）不得命中 ``search_files``。

    可能发现的缺陷：子串匹配把 ``search`` / ``file`` 等片段判为工具名命中。
    """

    assert (
        ModelToolHelper.invalid_tool_call_mention_tool_name(
            {"name": None, "args": "请读取并搜索文件 search 与 file"},
            {"search_files"},
        )
        is None
    )


def test_mention_tool_name_regex_special_chars_are_escaped() -> None:
    """测试目的：工具名含正则元字符时按字面量匹配（``re.escape`` 生效）。

    可能发现的缺陷：未转义工具名 → 正则语法错误抛 ``re.error``，或元字符被当
    通配符产生错误命中。
    """

    hit = ModelToolHelper.invalid_tool_call_mention_tool_name(
        {"name": None, "args": "use a.b tool"},
        {"a.b"},
    )
    miss = ModelToolHelper.invalid_tool_call_mention_tool_name(
        {"name": None, "args": "use axb tool"},
        {"a.b"},
    )

    assert hit == "a.b"
    assert miss is None


# =============================================================================
# 测试点 2：decide_invalid_tool_handling
# =============================================================================


def test_decide_invalid_tool_handling() -> None:
    """测试目的：覆盖双轨决策的四类契约——空列表双空、命中进 REPAIR（含
    tool_name+invalid_tool_call）、未命中进 IGNORE、混合列表两列表都非空。

    可能发现的缺陷（plan 用户核心场景）：
    - 空列表返回 ``{}`` 或缺键 → 调用方 ``result[REPAIR]`` 抛 KeyError；
    - 命中项被丢进 IGNORE 或字段名漂移（只存 tool_name 丢 args/error）；
    - 混合列表下遇到首个命中即 break、或 IGNORE 存在时抑制 REPAIR。
    """

    # 1) 空列表 → 双空列表且键完整
    empty_result = ModelToolHelper.decide_invalid_tool_handling(
        invalid_tool_calls=[],
        available_tool_names={"read_file"},
    )
    assert empty_result == {
        InvalidToolOutcome.IGNORE: [],
        InvalidToolOutcome.REPAIR: [],
    }

    # 2) 命中 → REPAIR 含 {tool_name, invalid_tool_call}
    hit = {"name": "read_file", "args": "{", "error": "bad json"}
    hit_result = ModelToolHelper.decide_invalid_tool_handling(
        invalid_tool_calls=[hit],
        available_tool_names={"read_file"},
    )
    assert hit_result[InvalidToolOutcome.IGNORE] == []
    assert hit_result[InvalidToolOutcome.REPAIR] == [
        {"tool_name": "read_file", "invalid_tool_call": hit}
    ]

    # 3) 未命中 → IGNORE（原样保留原始 payload 供排查）
    miss = {"name": "", "args": '"', "error": "Unterminated string"}
    miss_result = ModelToolHelper.decide_invalid_tool_handling(
        invalid_tool_calls=[miss],
        available_tool_names={"read_file"},
    )
    assert miss_result[InvalidToolOutcome.REPAIR] == []
    assert miss_result[InvalidToolOutcome.IGNORE] == [miss]

    # 4) 混合列表 → 两列表都非空且互不吞并
    noise = {"name": "", "args": "%%%", "error": "unparsable"}
    hit2 = {"name": None, "args": 'search_files(pattern="x"', "error": "truncated"}
    mixed_result = ModelToolHelper.decide_invalid_tool_handling(
        invalid_tool_calls=[noise, hit, hit2],
        available_tool_names={"read_file", "search_files"},
    )
    assert mixed_result[InvalidToolOutcome.IGNORE] == [noise]
    assert [item["tool_name"] for item in mixed_result[InvalidToolOutcome.REPAIR]] == [
        "read_file",
        "search_files",
    ]


def test_decide_all_ignore_when_available_tools_empty() -> None:
    """测试目的：可用工具集合为空时全部落 IGNORE（无从推断真实意图）。

    可能发现的缺陷：空工具集合下仍产出 REPAIR → 提示模型重发一个不存在的工具。
    """

    invalid_calls: list[dict[str, Any]] = [
        {"name": "read_file", "args": "{"},
        {"name": "search_files", "args": "["},
    ]
    result = ModelToolHelper.decide_invalid_tool_handling(
        invalid_tool_calls=invalid_calls,
        available_tool_names=set(),
    )

    assert result[InvalidToolOutcome.REPAIR] == []
    assert result[InvalidToolOutcome.IGNORE] == invalid_calls


# =============================================================================
# 测试点 3：build_invalid_tool_call_repair_message
# =============================================================================


def test_build_invalid_tool_call_repair_message() -> None:
    """测试目的：覆盖修复提示构造的六类契约——返回 str、含总领句、含 tool_name、
    args 预览 ≤500 截断、条目 >5 限条、redact_terminal_output 对 terminal 输出敏感串
    脱敏、整体总预算 ≤2000。

    可能发现的缺陷（plan §0.1 #4 / §4 / #6）：
    - 返回消息对象而非 str（``.content`` 类型 bug）；
    - 漏掉总领句/tool_name 使模型无法定位要修哪个工具；
    - 截断缺失或位置错误（先截断后脱敏 → secret 外泄）；
    - 限条数与总预算失效 → 大量残片全量入提示、撑爆上下文。
    """

    # 1) 基础：返回 str、含总领句与每条 tool_name
    message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=[
            _repair_data("read_file", args='{"path":', error="bad json"),
            _repair_data("search_files", args='{"pattern":', error="truncated"),
        ]
    )
    assert isinstance(message, str)
    assert "invalid tool call output that could not be parsed" in message
    assert "NOT executed" in message
    assert "read_file" in message
    assert "search_files" in message
    assert "error: bad json" in message
    assert "error: truncated" in message

    # 2) args 预览 ≤500 截断（超长被截 + ``...[truncated]`` 标记 + 不保留第 501 个字符）
    long_args = "A" * 5000
    long_message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=[_repair_data("read_file", args=long_args, error="too long")]
    )
    preview_limit = ModelToolHelper.INVALID_TOOL_ARGS_PREVIEW_CHARS
    assert "...[truncated]" in long_message
    assert "A" * preview_limit in long_message
    # 严格断言：截断后不得保留第 501 个连续 A
    assert "A" * (preview_limit + 1) not in long_message
    assert "error: too long" in long_message

    # 3) 条目 >5 限条（只输出前 INVALID_TOOL_CALL_SUMMARY_LIMIT 条）
    many_data = [
        _repair_data(f"tool_{i}", args=f"args_{i}", error=f"err_{i}")
        for i in range(8)
    ]
    limited = ModelToolHelper.build_invalid_tool_call_repair_message(repair_datas=many_data)
    limit = ModelToolHelper.INVALID_TOOL_CALL_SUMMARY_LIMIT
    assert limited.count("## tool_") == limit
    for i in range(limit):
        assert f"tool_{i}" in limited
    for i in range(limit, 8):
        assert f"tool_{i}" not in limited

    # 4) redact_terminal_output 脱敏（terminal 输出敏感串都被脱敏）
    # 用变量拼接规避静态分析的硬编码凭据误报（测试数据，非真实密钥）
    api_key_prefix = "sk-"
    api_key_secret = "abcdEFGHijklMNOP1234567890abcdef"
    token_secret = "token-xyz-987654321"
    secret_args = (
        f"$ export API_KEY={api_key_prefix}{api_key_secret} && "
        f"curl -H 'Authorization: Bearer {token_secret}' https://api.example.com"
    )
    redacted_message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=[
            _repair_data("execute_terminal", args=secret_args, error="bad json")
        ]
    )
    # 明文 sk- / token 凭据不得出现在提示中
    assert "sk-abcdEFGHijklMNOP1234567890abcdef" not in redacted_message
    assert "token-xyz-987654321" not in redacted_message
    assert "[REDACTED]" in redacted_message
    assert "execute_terminal" in redacted_message

    # 5) 整体总预算 ≤2000（多条长预览累加不得破预算）
    budget_data = [
        _repair_data(f"tool_{i}", args="B" * 480, error=f"err_{i}")
        for i in range(5)
    ]
    budget_message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=budget_data
    )
    assert len(budget_message) <= ModelToolHelper.INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS


def test_build_repair_message_respects_total_budget_and_keeps_key_fields() -> None:
    """测试目的：整体不超 2000 字符预算，且截断后每条仍含 tool_name 与 error。

    可能发现的缺陷：总预算未生效（多条 500 字符预览累加破 2000），或整体硬切
    产出半截 JSON、丢失可定位字段（plan §6.2 最后一条）。
    """

    repair_datas = [
        _repair_data(f"tool_{index}", args="B" * 480, error=f"err_{index}")
        for index in range(5)
    ]

    message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=repair_datas
    )

    assert len(message) <= ModelToolHelper.INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS
    # 已输出的每个条目都必须成对含 tool_name 与 error（非半截片段）
    for index in range(5):
        if f"## tool_{index}" in message:
            assert f"name: tool_{index}" in message
            assert f"error: err_{index}" in message


def test_build_repair_message_handles_empty_and_malformed_entries() -> None:
    """测试目的：空列表与非 dict 的 ``invalid_tool_call`` 均安全处理、不抛异常。

    可能发现的缺陷：对空列表返回 None、或对非 dict 残片直接 ``.get`` 抛
    AttributeError（LangChain 解析失败时可能是字符串）。
    """

    empty = ModelToolHelper.build_invalid_tool_call_repair_message(repair_datas=[])
    malformed = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=[{"tool_name": "read_file", "invalid_tool_call": "not-a-dict"}]
    )

    assert isinstance(empty, str)
    assert "NOT executed" in empty
    assert isinstance(malformed, str)
    assert "read_file" in malformed


def test_build_repair_message_omits_error_line_when_error_absent() -> None:
    """测试目的：无 ``error`` 字段时不输出空的 ``error:`` 行（不产生噪声）。

    可能发现的缺陷：无条件拼接 ``error: None`` / ``error: ``，让模型误读为错误原因。
    """

    message = ModelToolHelper.build_invalid_tool_call_repair_message(
        repair_datas=[_repair_data("read_file", args='{"path":')]
    )

    assert "read_file" in message
    assert "error:" not in message
