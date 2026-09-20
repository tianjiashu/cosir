from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats


def test_usage_metadata_replaces_previous_snapshot() -> None:
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata({
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_token_details": {"cache_read": 30},
    })
    assert stats.to_dict()["cache_miss_tokens"] == 70
    # 第二次调用以「覆盖更新」语义替换既有值，而非累加；
    # 最终生效的是最后一次 usage_metadata 的快照。
    stats.add_usage_metadata({
        "input_tokens": 50,
        "output_tokens": 5,
        "total_tokens": 55,
        "input_token_details": {"cache_read": 10},
    })
    assert stats.to_dict()["input_tokens"] == 50
    assert stats.to_dict()["cache_miss_tokens"] == 40


def test_cache_miss_is_hidden_when_provider_does_not_report_cache_details() -> None:
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    assert stats.to_dict()["cache_miss_tokens"] is None


def test_invalid_provider_token_values_are_ignored_without_interrupting_run() -> None:
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata({
        "input_tokens": -10,
        "output_tokens": 1.5,
        "total_tokens": float("inf"),
        "input_token_details": {"cache_read": -2},
        "output_token_details": {"reasoning": float("nan")},
    })

    assert stats.to_dict() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
    }
