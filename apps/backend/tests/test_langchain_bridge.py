"""runtime_to_langchain 转换的单元测试。

覆盖 2025 年修复的 OpenAI 400 协议校验 bug：
"assistant message with tool_calls must be followed by tool messages"。

根因 (B) 的防御性清洗在本模块 `runtime_to_langchain` 中：
- assistant 的 tool_calls 来自 ``metadata["tool_calls"]`` JSON 字符串；
- tool 消息的 id 来自 ``metadata["tool_call_id"]``；
- 没有对应 ToolMessage 的悬空 assistant tool_calls 会被剥离，并在剥离时记录 warning。

用例覆盖：
- 正常配对：assistant 带 2 个 tool_calls + 2 个对应 ToolMessage -> 两个都保留；
- 悬空剥离：assistant 带 3 个 tool_calls，仅 2 个有配对 -> 第 3 个被剥离；
- 普通 assistant/user/system 消息不受影响；
- tool_call_id 为空不崩溃。
"""

import json

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.llm.langchain_bridge import runtime_to_langchain
from app.models.runtime_message import RuntimeMessage


def _assistant(content: str, tool_calls: list[dict]) -> RuntimeMessage:
    return RuntimeMessage(
        role="assistant",
        content_text=content,
        metadata={"tool_calls": json.dumps(tool_calls)},
    )


def _tool(content: str, tool_call_id: str) -> RuntimeMessage:
    return RuntimeMessage(
        role="tool",
        content_text=content,
        metadata={"tool_call_id": tool_call_id},
    )


def _tc(name: str, call_id: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": call_id}


# --------------------------------------------------------------------------- #
# 正常配对：所有 assistant tool_calls 都有对应 ToolMessage
# --------------------------------------------------------------------------- #


def test_normal_pairing_preserves_all_tool_calls():
    """assistant 带 2 个 tool_calls，且各有对应 ToolMessage -> 两者都保留。"""
    messages = [
        _assistant(
            "calling tools",
            [_tc("search", "call-1", {"q": "a"}), _tc("read", "call-2", {"p": "b"})],
        ),
        _tool("result-1", "call-1"),
        _tool("result-2", "call-2"),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert len(converted) == 3
    assert isinstance(converted[0], AIMessage)
    assert len(converted[0].tool_calls) == 2
    # tool_calls 顺序与原始一致
    assert [tc["id"] for tc in converted[0].tool_calls] == ["call-1", "call-2"]
    # 后续两条 ToolMessage 配对正确
    assert isinstance(converted[1], ToolMessage) and converted[1].tool_call_id == "call-1"
    assert isinstance(converted[2], ToolMessage) and converted[2].tool_call_id == "call-2"


# --------------------------------------------------------------------------- #
# 悬空剥离：缺少对应 ToolMessage 的 assistant tool_calls 被剥离
# --------------------------------------------------------------------------- #


def test_orphan_tool_call_is_stripped():
    """assistant 带 3 个 tool_calls，仅 2 个有配对 -> 第 3 个被剥离。"""
    messages = [
        _assistant(
            "calling tools",
            [
                _tc("search", "call-1", {"q": "a"}),
                _tc("read", "call-2", {"p": "b"}),
                _tc("orphan", "call-3", {"x": "y"}),  # 无对应 ToolMessage
            ],
        ),
        _tool("result-1", "call-1"),
        _tool("result-2", "call-2"),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert isinstance(converted[0], AIMessage)
    # 只有配对的 2 个被保留，悬空的 call-3 被剥离
    assert len(converted[0].tool_calls) == 2
    assert [tc["id"] for tc in converted[0].tool_calls] == ["call-1", "call-2"]


def test_orphan_strip_also_drops_its_name():
    """被剥离的悬空调用不应出现在最终 AIMessage.tool_calls 里（含 name）。"""
    messages = [
        _assistant(
            "calling tools",
            [_tc("only_orphan", "call-x", {"k": "v"})],
        ),
        # 没有任何 ToolMessage 配对
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert isinstance(converted[0], AIMessage)
    assert converted[0].tool_calls == []


# --------------------------------------------------------------------------- #
# 无 tool_calls 的普通消息不受影响
# --------------------------------------------------------------------------- #


def test_plain_assistant_without_tool_calls_untouched():
    """无 tool_calls 元数据的普通 assistant 消息保持不变。"""
    messages = [
        RuntimeMessage(role="system", content_text="sys"),
        RuntimeMessage(role="user", content_text="hi"),
        RuntimeMessage(role="assistant", content_text="hello", metadata={}),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert len(converted) == 3
    assert isinstance(converted[0], SystemMessage) and converted[0].content == "sys"
    assert isinstance(converted[1], HumanMessage) and converted[1].content == "hi"
    assert isinstance(converted[2], AIMessage)
    assert converted[2].content == "hello"
    assert converted[2].tool_calls == []


def test_assistant_with_tool_calls_all_responding_no_orphan():
    """assistant 有 tool_calls 且全部配对时不应触发任何剥离。"""
    messages = [
        _assistant("t", [_tc("a", "id-1", {})]),
        _tool("r", "id-1"),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert isinstance(converted[0], AIMessage)
    assert len(converted[0].tool_calls) == 1
    assert converted[0].tool_calls[0]["id"] == "id-1"


# --------------------------------------------------------------------------- #
# 边界：tool_call_id 为空不崩溃
# --------------------------------------------------------------------------- #


def test_empty_tool_call_id_does_not_crash():
    """ToolMessage 的 tool_call_id 为空时不崩溃，仍生成 ToolMessage。"""
    messages = [
        _assistant("t", [_tc("a", "id-1", {})]),
        RuntimeMessage(role="tool", content_text="r", metadata={"tool_call_id": ""}),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)

    assert isinstance(converted[1], ToolMessage)
    assert converted[1].tool_call_id == ""
    # 空 id 不在 responded_ids 中，故 assistant 的 call 会被当作悬空剥离
    assert isinstance(converted[0], AIMessage)
    assert converted[0].tool_calls == []


def test_missing_tool_call_id_metadata_does_not_crash():
    """ToolMessage 完全没有 tool_call_id 元数据时不崩溃。"""
    messages = [
        RuntimeMessage(role="tool", content_text="r", metadata={}),
    ]
    converted: list[BaseMessage] = runtime_to_langchain(messages)
    assert len(converted) == 1
    assert isinstance(converted[0], ToolMessage)
    assert converted[0].tool_call_id == ""


# --------------------------------------------------------------------------- #
# 边界：空 / 非法 metadata 的 tool_calls 不影响普通转换
# --------------------------------------------------------------------------- #


def test_malformed_tool_calls_metadata_does_not_crash():
    """assistant 的 tool_calls 元数据为空串/非法 JSON 时按无 tool_calls 处理。"""
    for bad_meta in ("", "not-json", "{}", "42"):
        messages = [
            RuntimeMessage(
                role="assistant",
                content_text="hi",
                metadata={"tool_calls": bad_meta},
            ),
        ]
        converted: list[BaseMessage] = runtime_to_langchain(messages)
        assert isinstance(converted[0], AIMessage)
        assert converted[0].tool_calls == []
