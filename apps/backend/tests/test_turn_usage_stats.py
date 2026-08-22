"""``TurnUsageStats`` 单元测试。

验证 usage 单一来源改造后的累加契约：

- ``add_usage_metadata`` 按 LangChain ``UsageMetadata`` 标准嵌套契约解析
  （input_token_details.cache_read / output_token_details.reasoning），取代旧
  扁平键 ``prompt_cache_hit_tokens`` / ``reasoning_tokens`` 的键名偏差。
- 多步模型调用（含 REPAIR 回流）依次累加，互不覆盖。
- 委托别名 ``add_message_usage`` 行为一致。
- None / 空字典 / 字段缺失安全忽略。
"""

from app.models.turn_usage_stats import TurnUsageStats


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cache_read: int = 0,
    reasoning: int = 0,
) -> dict:
    """构造一个 LangChain ``UsageMetadata`` 形态字典，供测试复用。"""
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_token_details": {"cache_read": cache_read},
        "output_token_details": {"reasoning": reasoning},
    }


def test_add_usage_metadata_parses_nested_contract() -> None:
    """add_usage_metadata 应解析 UsageMetadata 嵌套细节，而非旧扁平键。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(_usage(10, 4, 14, cache_read=6, reasoning=2))

    assert stats.input_tokens == 10
    assert stats.output_tokens == 4
    assert stats.total_tokens == 14
    # 缓存命中来自 input_token_details.cache_read，而非已废弃的 prompt_cache_hit_tokens
    assert stats.cache_hit_tokens == 6
    # 推理 token 来自 output_token_details.reasoning
    assert stats.reasoning_tokens == 2


def test_add_usage_metadata_accumulates_across_steps() -> None:
    """多步模型调用（REPAIR 回流）依次累加，末态不覆盖前步。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(_usage(10, 4, 14, cache_read=6, reasoning=2))
    stats.add_usage_metadata(_usage(5, 3, 8, cache_read=1, reasoning=1))

    assert stats.input_tokens == 15
    assert stats.output_tokens == 7
    assert stats.total_tokens == 22
    assert stats.cache_hit_tokens == 7
    assert stats.reasoning_tokens == 3


def test_add_usage_metadata_none_is_safe() -> None:
    """usage_metadata 为 None 时不累加、不抛错。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(None)
    assert stats.to_dict() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_add_usage_metadata_empty_dict_is_safe() -> None:
    """usage_metadata 为空字典时不累加、不抛错。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata({})
    assert stats.input_tokens == 0
    assert stats.reasoning_tokens == 0


def test_add_usage_metadata_missing_details_default_zero() -> None:
    """缺失 input_token_details / output_token_details 时缓存与推理计为零。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata({"input_tokens": 7, "output_tokens": 2, "total_tokens": 9})

    assert stats.input_tokens == 7
    assert stats.output_tokens == 2
    assert stats.total_tokens == 9
    assert stats.cache_hit_tokens == 0
    assert stats.reasoning_tokens == 0


def test_add_usage_metadata_invalid_field_value_skipped() -> None:
    """字段值类型异常（如字符串）时安全跳过该字段，不抛错。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(
        {"input_tokens": "not-a-number", "output_tokens": 3, "total_tokens": 3}
    )
    assert stats.input_tokens == 0
    assert stats.output_tokens == 3


def test_add_usage_metadata_none_input_details_is_safe() -> None:
    """嵌套 input_token_details 为 None 时不应抛 AttributeError，cache 计为零（边界漏洞核查）。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_token_details": None,
            "output_token_details": {"reasoning": 2},
        }
    )

    assert stats.input_tokens == 10
    assert stats.output_tokens == 4
    assert stats.total_tokens == 14
    # input_token_details=None 经由 `or {}` 回退，cache 不应崩溃并计为零
    assert stats.cache_hit_tokens == 0
    assert stats.reasoning_tokens == 2


def test_add_usage_metadata_none_output_details_is_safe() -> None:
    """嵌套 output_token_details 为 None 时不应抛 AttributeError，reasoning 计为零（边界漏洞核查）。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_token_details": {"cache_read": 6},
            "output_token_details": None,
        }
    )

    assert stats.input_tokens == 10
    assert stats.output_tokens == 4
    assert stats.total_tokens == 14
    assert stats.cache_hit_tokens == 6
    # output_token_details=None 经由 `or {}` 回退，reasoning 不应崩溃并计为零
    assert stats.reasoning_tokens == 0


def test_add_usage_metadata_non_dict_details_is_skipped() -> None:
    """嵌套 details 为非 dict 对象（如字符串/数字）时应安全跳过，不抛错（边界漏洞核查）。"""
    stats = TurnUsageStats()
    stats.add_usage_metadata(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "input_token_details": "not-a-dict",  # 非 dict，truthy → isinstance 检查失败 → 跳过
            "output_token_details": 123,  # 非 dict → 跳过
        }
    )

    assert stats.input_tokens == 10
    assert stats.output_tokens == 4
    assert stats.total_tokens == 14
    assert stats.cache_hit_tokens == 0
    assert stats.reasoning_tokens == 0


def test_add_message_usage_alias_matches_add_usage_metadata() -> None:
    """add_message_usage 应委托 add_usage_metadata，行为完全一致。"""
    delegated = TurnUsageStats()
    direct = TurnUsageStats()

    delegated.add_message_usage(_usage(10, 4, 14, cache_read=6, reasoning=2))
    direct.add_usage_metadata(_usage(10, 4, 14, cache_read=6, reasoning=2))

    assert delegated.to_dict() == direct.to_dict()
