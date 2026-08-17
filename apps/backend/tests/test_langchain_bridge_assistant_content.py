"""``langchain_bridge.sanitize_assistant_messages`` 单元测试。

锁定不变量（与当前实现一致，全部为可观测字段断言，不依赖任何 provider 私有符号）：

- assistant 消息 content 为**空字符串**时被归一化为非空纯空白占位符（根除序列化后
  ``content: null`` 被 DeepSeek 拒绝的问题）；
- ``AIMessageChunk``（checkpoint 恢复未合并分片）归一化为普通 ``AIMessage``；
- 重建时仅保留 ``content`` + 合法 ``tool_calls`` + ``id``：``additional_kwargs``
  （含裸 ``tool_calls``/``function_call``/``reasoning_content``/``reasoning`` 等危险键）、
  ``invalid_tool_calls``、``response_metadata`` 一律不随历史消息回灌下一轮；
- 非 assistant 角色（``ToolMessage``/``SystemMessage``/``HumanMessage``）保持原对象引用不变。

注意：``content=[]``（空列表）与残缺 ``tool_calls``（name/args/id 为 null）属已知既有缺陷
（``sanitize_assistant_messages`` 会分别抛 ``AttributeError`` / pydantic ``ValidationError``），
不在本文件锁定其行为，详见交付报告「发现的业务代码问题」。
"""

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.llm.langchain_bridge import sanitize_assistant_messages


def _valid_tool_call(id_: str = "call_1", name: str = "read_file") -> dict:
    """构造一条合法的 OpenAI 函数 tool_call（含 type 标记，模拟流式合并后的形态）。"""
    return {"name": name, "args": {}, "id": id_, "type": "tool_call"}


def test_empty_assistant_content_normalized_to_placeholder() -> None:
    """assistant 消息 content 为空字符串时，归一化为非空纯空白占位符。"""
    messages = [AIMessage(content="", tool_calls=[_valid_tool_call()])]

    normalized = sanitize_assistant_messages(messages)

    assert isinstance(normalized[0], AIMessage)
    assert normalized[0].content != ""
    assert normalized[0].content.strip() == ""  # 纯空白占位，序列化后非 null
    assert normalized[0].content is not None


def test_ai_message_chunk_normalized_to_ai_message() -> None:
    """AIMessageChunk（checkpoint 恢复未合并分片）也归一化为普通 AIMessage 占位。"""
    messages = [AIMessageChunk(content="", tool_calls=[_valid_tool_call()])]

    normalized = sanitize_assistant_messages(messages)

    assert isinstance(normalized[0], AIMessage)
    assert not isinstance(normalized[0], AIMessageChunk)
    assert normalized[0].content != ""
    assert normalized[0].content.strip() == ""


def test_non_empty_assistant_content_preserved() -> None:
    """content 非空且 tool_calls 合法的 assistant 消息保持原样，不引入多余占位。"""
    messages = [
        AIMessage(content="请先查看文件", tool_calls=[_valid_tool_call(id_="1")])
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "请先查看文件"
    assert normalized[0].tool_calls[0]["id"] == "1"
    assert normalized[0].tool_calls[0]["name"] == "read_file"


def test_assistant_id_preserved_through_roundtrip() -> None:
    """重建后的 assistant 消息保留原消息的 id（id 是协议必须的稳定标识）。"""
    messages = [AIMessage(content="保留 id", id="assistant-msg-1")]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].id == "assistant-msg-1"


def test_legal_tool_calls_preserved_through_roundtrip() -> None:
    """合法 tool_calls 原样透传：id/name/args/type 均保留。"""
    messages = [AIMessage(content="调用工具", tool_calls=[_valid_tool_call(id_="call_ok")])]

    normalized = sanitize_assistant_messages(messages)

    tool_calls = normalized[0].tool_calls
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_ok"
    assert tool_calls[0]["name"] == "read_file"
    assert tool_calls[0]["args"] == {}
    assert tool_calls[0]["type"] == "tool_call"


def test_additional_kwargs_dangerous_keys_dropped() -> None:
    """additional_kwargs 的裸 tool_calls/function_call/reasoning_content/reasoning 等入口守卫剥离。

    这些键来自当轮模型自动累积（流式残留、推理思考过程、旧式 function_call），重建时不传
    ``additional_kwargs``，因此整块清空（含无害键），绝不作为历史回灌下一轮。
    """
    messages = [
        AIMessage.model_construct(
            content="思考完毕",
            tool_calls=[_valid_tool_call(id_="call_ok")],
            additional_kwargs={
                "tool_calls": [{"id": "stale", "type": "function", "function": {"name": "x"}}],
                "function_call": {"name": "legacy", "arguments": "{}"},
                "reasoning_content": "我是思考过程",
                "reasoning": "冗余推理",
                "audio": {"id": "keep-me"},
            },
            response_metadata={"finish_reason": "stop", "model_name": "deepseek"},
            invalid_tool_calls=[{"name": "bad", "args": "{}", "id": "x"}],
        )
    ]

    normalized = sanitize_assistant_messages(messages)

    # 重建不传 additional_kwargs：危险键与无害键均不保留
    assert normalized[0].additional_kwargs == {}
    for dangerous in ("tool_calls", "function_call", "reasoning_content", "reasoning", "audio"):
        assert dangerous not in normalized[0].additional_kwargs
    # 合法 tool_calls 不受影响（经访问器读取）
    assert len(normalized[0].tool_calls) == 1
    assert normalized[0].tool_calls[0]["id"] == "call_ok"


def test_invalid_tool_calls_and_response_metadata_dropped() -> None:
    """上一轮残留的 invalid_tool_calls 与 response_metadata 是当轮解析噪声/本地元数据，绝不应回灌。

    构造带脏字段但 content/tool_calls 合法的 AIMessage，验证归一化后这两个字段被清空，
    避免向端点提交上一轮的脏字段、污染下一轮上下文。
    """
    messages = [
        AIMessage.model_construct(
            content="我来调用工具",
            tool_calls=[_valid_tool_call(id_="call_ok")],
            additional_kwargs={},
            response_metadata={"model_name": "deepseek", "finish_reason": "tool_calls"},
            invalid_tool_calls=[
                {"name": "read_file", "args": '{"path":', "id": "bad", "error": "truncated json"}
            ],
        )
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "我来调用工具"
    assert len(normalized[0].tool_calls) == 1
    # 关键不变量：invalid_tool_calls / response_metadata 不回灌下一轮
    assert getattr(normalized[0], "invalid_tool_calls", None) in (None, [])
    assert getattr(normalized[0], "response_metadata", None) in (None, {})


def test_empty_content_with_tool_calls_gets_placeholder() -> None:
    """assistant 携带 tool_calls 但 content 为空串时，content 归一化为占位而 tool_calls 保留。

    这是 DeepSeek 协议拒绝（``content: null``）的最常见现场：工具调用轮无文本回复。
    """
    messages = [AIMessage(content="", tool_calls=[_valid_tool_call(id_="t1")])]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content != ""
    assert normalized[0].content.strip() == ""
    assert normalized[0].tool_calls[0]["id"] == "t1"


def test_tool_message_untouched() -> None:
    """ToolMessage 保持原对象引用（其 content=null 在协议上合法，不应被归一化改动）。"""
    messages = [ToolMessage(content="ok", tool_call_id="1")]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0] is messages[0]
    assert normalized[0].content == "ok"


def test_system_and_human_messages_preserved() -> None:
    """system / human 消息不受归一化影响，保持原对象引用；assistant 仍归一化。"""
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[]),
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0] is messages[0]
    assert normalized[0].content == "sys"
    assert normalized[1] is messages[1]
    assert normalized[1].content == "hi"
    assert isinstance(normalized[2], AIMessage)
    assert normalized[2].content != ""
    assert normalized[2].content.strip() == ""
