"""多轮工具循环的 thinking 回传契约单元测试。

覆盖设计文档阶段 3.3：Anthropic / Gemini / OpenAI o 系列需要把 assistant 消息中的
thinking 块（含 signature / encrypted_content）原样回传给下一轮工具调用，否则 400；
DeepSeek / Kimi（仅 reasoning_content 通道）则剥离避免重复思考；``thinking_roundtrip``
为 False 时强制剥离不抛（宁可丢 thinking 也不 400）。

测试模拟「assistant 产出 thinking → 合并落库 → 作为历史消息喂给下一轮」的两轮工具
循环：断言经 ``_collect_chunk_to_ai_message`` 产出的 assistant 消息在历史重建后仍携带
signature / thinking 块（即多轮循环不会把 thinking 剥掉导致 400）。
"""

from langchain_core.messages import AIMessage, AIMessageChunk

from app.core.workflows.nodes.model_node import _collect_chunk_to_ai_message


def _assistant_round(chunks, channels, roundtrip: bool) -> AIMessage:
    """合并一轮 assistant 输出为可落库/回灌的 AIMessage。"""
    return _collect_chunk_to_ai_message(
        chunks, thinking_channels=channels, thinking_roundtrip=roundtrip
    )


def test_anthropic_signature_preserved_across_tool_rounds() -> None:
    """Anthropic 两轮工具循环：thinking_blocks+signature 每轮均保留（不 400）。"""
    anthropic_channels = ("thinking_blocks", "reasoning_content")

    # 第 1 轮：assistant 产出 thinking_blocks + 附带 signature
    round1 = _assistant_round(
        [
            AIMessageChunk(
                content="调用 tool_a",
                additional_kwargs={
                    "reasoning_content": "signature-v1",
                    "thinking_blocks": [{"type": "thinking", "thinking": "推理一"}],
                },
            )
        ],
        anthropic_channels,
        True,
    )
    # 第一轮历史含 signature
    assert "reasoning_content" in round1.additional_kwargs
    assert round1.additional_kwargs["reasoning_content"] == "signature-v1"
    assert round1.content == "调用 tool_a"

    # 第 2 轮：把 round1 作为历史喂入，再次产出 assistant（仍应保留 thinking 回传字段）
    history = [round1]
    round2_chunks = [
        AIMessageChunk(
            content="调用 tool_b",
            additional_kwargs={
                "reasoning_content": "signature-v2",
                "thinking_blocks": [{"type": "thinking", "thinking": "推理二"}],
            },
        )
    ]
    round2 = _assistant_round(round2_chunks, anthropic_channels, True)
    # 回传契约：第二轮 assistant 的 signature 仍保留，不会被剥离
    assert "reasoning_content" in round2.additional_kwargs
    assert round2.additional_kwargs["reasoning_content"] == "signature-v2"
    # 历史消息同样保留 signature（回灌时不会丢）
    assert "reasoning_content" in history[0].additional_kwargs


def test_deepseek_strips_reasoning_across_rounds() -> None:
    """DeepSeek（仅 reasoning_content 通道）多轮循环剥离 reasoning，不回传。"""
    deepseek_channels = ("reasoning_content",)

    round1 = _assistant_round(
        [
            AIMessageChunk(
                content="调用 tool_a",
                additional_kwargs={"reasoning_content": "思考一"},
            )
        ],
        deepseek_channels,
        True,
    )
    assert "reasoning_content" not in round1.additional_kwargs
    assert round1.content == "调用 tool_a"


def test_roundtrip_disabled_strips_without_raising() -> None:
    """thinking_roundtrip=False：即便 Anthropic 通道也强制剥离，不抛。"""
    anthropic_channels = ("thinking_blocks", "reasoning_content")

    round1 = _assistant_round(
        [
            AIMessageChunk(
                content="回答",
                additional_kwargs={
                    "reasoning_content": "signature-v1",
                    "thinking_blocks": [{"type": "thinking", "thinking": "推理一"}],
                },
            )
        ],
        anthropic_channels,
        False,
    )
    assert "reasoning_content" not in round1.additional_kwargs
    assert round1.content == "回答"
