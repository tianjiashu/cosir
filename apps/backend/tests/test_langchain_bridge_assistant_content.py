"""``langchain_bridge.sanitize_assistant_messages`` 单元测试。

锁定不变量：提交给模型前，assistant 消息的 content 空值会被非空占位，且 tool_calls 中
id/name 为空的非法条目会被过滤；经 langchain-openai 序列化后不再触发 DeepSeek 的
``expected a string`` / ``content: null`` 400 错误。ToolMessage 等其他角色不受影响。
"""

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.core.llm.langchain_bridge import _ADDITIONAL_KWARGS_DROP_KEYS, sanitize_assistant_messages


def _serialize_to_provider(message: object) -> dict:
    """复用 langchain-openai 内部序列化，模拟消息最终提交给模型端点的形态。"""
    from langchain_openai.chat_models.base import _convert_message_to_dict

    return _convert_message_to_dict(message)


def test_empty_assistant_content_normalized_to_placeholder() -> None:
    """assistant 消息 content 为空串时，归一化为非空占位符。"""
    messages = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "1"}])
    ]

    normalized = sanitize_assistant_messages(messages)

    assert isinstance(normalized[0], AIMessage)
    assert normalized[0].content != ""
    assert normalized[0].content is not None


def test_empty_list_assistant_content_normalized_to_placeholder() -> None:
    """assistant 消息 content 为空列表 ``[]`` 时归一化为非空占位（覆盖序列化为 null 的变体）。"""
    messages = [
        AIMessage(content=[], tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "1"}])
    ]

    normalized = sanitize_assistant_messages(messages)

    assert isinstance(normalized[0], AIMessage)
    assert normalized[0].content != ""
    assert normalized[0].content is not None


def test_ai_message_chunk_normalized_to_ai_message() -> None:
    """AIMessageChunk（checkpoint 恢复未合并分片）也归一化为普通 AIMessage 占位。"""
    messages = [
        AIMessageChunk(content="", tool_calls=[{"name": "read_file", "args": {}, "id": "1"}])
    ]

    normalized = sanitize_assistant_messages(messages)

    assert isinstance(normalized[0], AIMessage)
    assert not isinstance(normalized[0], AIMessageChunk)
    assert normalized[0].content != ""


def test_serialized_assistant_content_is_not_null() -> None:
    """归一化后 assistant 消息提交给 DeepSeek 序列化结果 content 不为 null。"""
    messages = [
        AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "1"}])
    ]

    normalized = sanitize_assistant_messages(messages)
    serialized = _serialize_to_provider(normalized[0])

    assert serialized["role"] == "assistant"
    assert serialized["content"] is not None
    assert serialized["content"] != ""


def _make_ai_message_with_broken_tool_calls() -> AIMessage:
    """构造带残缺 tool_call 的 AIMessage，模拟流式合并后的异常形态。

    LangChain 的 ``AIMessage`` 构造器会拒绝 ``name=None``，因此用 ``model_construct``
    绕过 pydantic 校验，复现 checkpoint/流式合并中可能残留的非法 tool_call。
    """
    return AIMessage.model_construct(
        content="我来探索代码结构",
        tool_calls=[
            {
                "name": "list_directory",
                "args": {"path": "."},
                "id": "call_valid",
                "type": "tool_call",
            },
            {"name": None, "args": "\"", "id": None, "type": "tool_call"},
        ],
        additional_kwargs={},
        response_metadata={},
        invalid_tool_calls=[],
    )


def test_tool_calls_with_null_id_and_name_are_filtered() -> None:
    """过滤 id/name 为 null 的残缺 tool_call，避免序列化后触发 expected a string 400。"""
    messages = [_make_ai_message_with_broken_tool_calls()]

    normalized = sanitize_assistant_messages(messages)

    assert len(normalized[0].tool_calls) == 1
    assert normalized[0].tool_calls[0]["id"] == "call_valid"
    assert normalized[0].tool_calls[0]["name"] == "list_directory"


def test_serialized_tool_calls_have_no_null_id_or_name() -> None:
    """清洗后序列化的 tool_calls 不再包含 null id/name。"""
    messages = [_make_ai_message_with_broken_tool_calls()]

    normalized = sanitize_assistant_messages(messages)
    serialized = _serialize_to_provider(normalized[0])

    assert serialized["role"] == "assistant"
    assert serialized["content"] is not None
    tool_calls = serialized.get("tool_calls", [])
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_valid"
    assert tool_calls[0]["function"]["name"] == "list_directory"


def test_tool_message_untouched() -> None:
    """ToolMessage 的 content 不应被归一化改动（其 content=null 在协议上合法）。"""
    messages = [ToolMessage(content="ok", tool_call_id="1")]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "ok"
    assert _serialize_to_provider(normalized[0])["content"] == "ok"


def test_non_empty_assistant_content_preserved() -> None:
    """content 非空且 tool_calls 合法的 assistant 消息保持原样，不引入多余占位。"""
    messages = [
        AIMessage(content="请先查看文件", tool_calls=[{"name": "read_file", "args": {}, "id": "1"}])
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "请先查看文件"
    assert normalized[0].tool_calls[0]["name"] == "read_file"


def test_invalid_tool_calls_dropped_on_roundtrip() -> None:
    """上一轮残留的 invalid_tool_calls 是当轮解析噪声，绝不应随历史消息回灌下一轮对话。

    构造带 invalid_tool_calls 的 AIMessage（content 合法、tool_calls 合法，模拟「无需清洗但
    携带脏字段」的历史消息），验证归一化后 invalid_tool_calls 被清空，避免向端点提交上一轮
    的脏字段、污染下一轮上下文。
    """
    messages = [
        AIMessage.model_construct(
            content="我来调用工具",
            tool_calls=[{"name": "read_file", "args": {}, "id": "call_ok", "type": "tool_call"}],
            additional_kwargs={},
            response_metadata={},
            invalid_tool_calls=[
                {"name": "read_file", "args": '{"path":', "id": "bad", "error": "truncated json"}
            ],
        )
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "我来调用工具"
    assert len(normalized[0].tool_calls) == 1
    # 关键不变量：invalid_tool_calls 不回灌下一轮
    assert getattr(normalized[0], "invalid_tool_calls", None) in (None, [])


def test_system_and_human_messages_preserved() -> None:
    """system / human 消息不受归一化影响。"""
    messages = [
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        AIMessage(content="", tool_calls=[]),
    ]

    normalized = sanitize_assistant_messages(messages)

    assert normalized[0].content == "sys"
    assert normalized[1].content == "hi"
    assert isinstance(normalized[2], AIMessage)
    assert normalized[2].content != ""


def test_additional_kwargs_dangerous_keys_dropped() -> None:
    """additional_kwargs 的裸 tool_calls/function_call/reasoning_content/reasoning 须入口守卫剥离。

    这些键来自当轮模型自动累积（流式残留、推理思考过程、旧式 function_call），langchain-openai
    序列化时会直接拼进请求（不经验证）或纯属内部噪声，绝不应作为历史回灌下一轮。
    """
    messages = [
        AIMessage.model_construct(
            content="思考完毕",
            tool_calls=[{"name": "read_file", "args": {}, "id": "call_ok", "type": "tool_call"}],
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

    dropped = _ADDITIONAL_KWARGS_DROP_KEYS
    kept = dict(normalized[0].additional_kwargs)
    for key in dropped:
        assert key not in kept, f"危险键 {key} 未被剥离"
    # 无害键 audio 应保留
    assert kept.get("audio") == {"id": "keep-me"}
    # 合法 tool_calls 不受影响
    assert len(normalized[0].tool_calls) == 1
    # 顶层 invalid_tool_calls 与 response_metadata 均不回灌
    assert getattr(normalized[0], "invalid_tool_calls", None) in (None, [])
    assert getattr(normalized[0], "response_metadata", None) in (None, {})


def test_dropped_keys_not_serialized_to_provider() -> None:
    """剥离后的消息序列化给端点时不包含 dangerous/reasoning 键，避免非标准键触发拒绝。"""
    messages = [
        AIMessage.model_construct(
            content="回答",
            tool_calls=[],
            additional_kwargs={"reasoning_content": "secret-thinking", "tool_calls": [{"id": "x"}]},
            response_metadata={"model_name": "deepseek"},
        )
    ]

    normalized = sanitize_assistant_messages(messages)
    serialized = _serialize_to_provider(normalized[0])

    assert "reasoning_content" not in serialized
    assert "tool_calls" not in serialized  # 已清空裸残留且无合法 tool_calls
    assert "function_call" not in serialized
    assert serialized["content"] == "回答"
