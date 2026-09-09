from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats


def test_cache_miss_tokens_are_derived_from_cumulative_input_and_hits() -> None:
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata({
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_token_details": {"cache_read": 30},
    })
    stats.add_usage_metadata({
        "input_tokens": 50,
        "output_tokens": 5,
        "total_tokens": 55,
        "input_token_details": {"cache_read": 10},
    })
    assert stats.to_dict()["cache_miss_tokens"] == 110


def test_cache_miss_is_hidden_when_provider_does_not_report_cache_details() -> None:
    stats = ConversationRunUsageStats()
    stats.add_usage_metadata({"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    assert stats.to_dict()["cache_miss_tokens"] == 0
