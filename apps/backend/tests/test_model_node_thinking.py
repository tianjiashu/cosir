"""model_node thinking 多通道抽取与剥离策略单元测试。

覆盖设计文档阶段 3.2/3.3 契约：
- ``_extract_reasoning_content`` 按 ``LLMRuntimeConfig.thinking_channels``
  分派各厂商通道（reasoning_content / thought / thinking_blocks / reasoning），
  空通道或字段缺失安全返回空串；
- ``_collect_chunk_to_ai_message`` 按回传策略剥离或保留 ``reasoning_content``：
  仅 ``reasoning_content`` 通道厂商（DeepSeek/Kimi）剥离；含 thinking_blocks /
  thought / reasoning 通道厂商（Anthropic/Gemini/o 系列）保留；
  ``thinking_roundtrip=False`` 强制剥离不抛。
"""

from langchain_core.messages import AIMessage, AIMessageChunk

from app.core.workflows.nodes.model_node import (
    _collect_chunk_to_ai_message,
    _extract_reasoning_content,
    _should_strip_reasoning_content,
)


# --------------------------------------------------------------------------- #
# _extract_reasoning_content：多通道抽取
# --------------------------------------------------------------------------- #
def test_extract_reasoning_content_channel() -> None:
    """reasoning_content 通道：读 additional_kwargs["reasoning_content"]。"""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning_content": "思考中…"},
    )
    assert _extract_reasoning_content(chunk, ("reasoning_content",)) == "思考中…"


def test_extract_thought_channel() -> None:
    """thought 通道：遍历 content 块中 thought=True 的文本（Gemini）。"""
    chunk = AIMessageChunk(
        content=[
            {"type": "text", "text": "可见文本"},
            {"type": "text", "text": "隐藏思考", "thought": True},
        ],
    )
    assert _extract_reasoning_content(chunk, ("thought",)) == "隐藏思考"


def test_extract_thinking_blocks_channel() -> None:
    """thinking_blocks 通道：读 type=thinking 块文本（Anthropic）。"""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={
            "thinking_blocks": [
                {"type": "thinking", "thinking": "推理过程"},
                {"type": "redacted_thinking", "data": "x"},
            ]
        },
    )
    assert _extract_reasoning_content(chunk, ("thinking_blocks",)) == "推理过程"


def test_extract_reasoning_channel_summary() -> None:
    """reasoning 通道：读 additional_kwargs["reasoning"] 摘要（OpenAI o 系列）。"""
    chunk = AIMessageChunk(
        content="",
        additional_kwargs={"reasoning": "推理摘要"},
    )
    assert _extract_reasoning_content(chunk, ("reasoning",)) == "推理摘要"


def test_extract_empty_channels_returns_empty() -> None:
    """空通道元组：不抽取，返回空串。"""
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": "x"})
    assert _extract_reasoning_content(chunk, ()) == ""


def test_extract_missing_field_returns_empty() -> None:
    """字段缺失安全返回空串（不抛）。"""
    chunk = AIMessageChunk(content="")
    assert _extract_reasoning_content(chunk, ("reasoning_content",)) == ""


def test_extract_non_string_value_returns_empty() -> None:
    """reasoning_content 为非字符串时返回空串（不抛）。"""
    chunk = AIMessageChunk(content="", additional_kwargs={"reasoning_content": 123})
    assert _extract_reasoning_content(chunk, ("reasoning_content",)) == ""


# --------------------------------------------------------------------------- #
# _should_strip_reasoning_content：回传策略判定
# --------------------------------------------------------------------------- #
def test_strip_only_reasoning_content_channel() -> None:
    """仅 reasoning_content 通道（DeepSeek/Kimi）→ 剥离。"""
    assert _should_strip_reasoning_content(("reasoning_content",), True) is True


def test_preserve_when_roundtrip_required_channel() -> None:
    """含 thinking_blocks（Anthropic）→ 保留（剥离 False）。"""
    assert _should_strip_reasoning_content(("thinking_blocks", "reasoning_content"), True) is False


def test_preserve_thought_and_reasoning_channels() -> None:
    """thought（Gemini）/ reasoning（OpenAI o）→ 保留。"""
    assert _should_strip_reasoning_content(("thought",), True) is False
    assert _should_strip_reasoning_content(("reasoning",), True) is False


def test_force_strip_when_roundtrip_disabled() -> None:
    """thinking_roundtrip=False → 强制剥离，即便含回传通道。"""
    assert _should_strip_reasoning_content(("thinking_blocks", "reasoning_content"), False) is True


def test_strip_when_empty_channels_legacy_default() -> None:
    """通道为空（未解析到 LLMRuntimeConfig）→ 保持既有剥离行为（兼容旧路径）。"""
    assert _should_strip_reasoning_content((), True) is True


# --------------------------------------------------------------------------- #
# _collect_chunk_to_ai_message：剥离/保留落地
# --------------------------------------------------------------------------- #
def test_collect_strips_reasoning_content_for_deepseek() -> None:
    """DeepSeek（仅 reasoning_content 通道）→ 合并后剥离 reasoning_content。"""
    chunks = [
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "思考"}),
        AIMessageChunk(content="最终回答"),
    ]
    msg = _collect_chunk_to_ai_message(
        chunks, thinking_channels=("reasoning_content",), thinking_roundtrip=True
    )
    assert isinstance(msg, AIMessage)
    assert "reasoning_content" not in msg.additional_kwargs
    assert msg.content == "最终回答"


def test_collect_preserves_reasoning_content_for_anthropic() -> None:
    """Anthropic（thinking_blocks 通道）→ 保留 reasoning_content（含 signature 回传）。"""
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_content": "signature-data",
                "thinking_blocks": [{"type": "thinking", "thinking": "推理"}],
            },
        ),
        AIMessageChunk(content="回答"),
    ]
    msg = _collect_chunk_to_ai_message(
        chunks,
        thinking_channels=("thinking_blocks", "reasoning_content"),
        thinking_roundtrip=True,
    )
    assert "reasoning_content" in msg.additional_kwargs
    assert msg.additional_kwargs["reasoning_content"] == "signature-data"


def test_collect_preserves_for_gemini_thought() -> None:
    """Gemini（thought 通道）→ 保留 reasoning_content（未剥离）。"""
    chunks = [AIMessageChunk(content="", additional_kwargs={"reasoning_content": "t"})]
    msg = _collect_chunk_to_ai_message(
        chunks, thinking_channels=("thought",), thinking_roundtrip=True
    )
    assert "reasoning_content" in msg.additional_kwargs


def test_collect_force_strip_when_roundtrip_disabled() -> None:
    """thinking_roundtrip=False → 强制剥离，不抛（即便含回传通道）。"""
    chunks = [
        AIMessageChunk(
            content="",
            additional_kwargs={"reasoning_content": "sig", "thinking_blocks": []},
        ),
        AIMessageChunk(content="回答"),
    ]
    msg = _collect_chunk_to_ai_message(
        chunks,
        thinking_channels=("thinking_blocks", "reasoning_content"),
        thinking_roundtrip=False,
    )
    assert "reasoning_content" not in msg.additional_kwargs
    assert msg.content == "回答"


def test_collect_empty_chunks_returns_empty_message() -> None:
    """空 chunk 列表返回空 content 的 AIMessage。"""
    msg = _collect_chunk_to_ai_message([], thinking_channels=(), thinking_roundtrip=True)
    assert isinstance(msg, AIMessage)
    assert msg.content == ""
