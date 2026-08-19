"""模型调用成本估算（cost_estimator）单元测试。

覆盖设计文档阶段 5 ②契约：
- 已知模型按 litellm 价格表计算成本（美分）；
- 未知模型返回 None（不估算）；
- 缓存命中 token 按折扣价计；
- 输出侧把 reasoning_tokens 并入 output 计费；
- 价格字段缺失 / 查询异常回退 None（不抛）。
"""

from unittest.mock import patch

import pytest

from app.service.llm.cost_estimator import UsageBreakdown, estimate_cost

#: 模拟 litellm 价格表：deepseek-chat 每 1M token 输入 0.28 美元、输出 0.42 美元、
#: 缓存命中输入 0.028 美元。
_FAKE_MODEL_COST: dict = {
    "deepseek/deepseek-chat": {
        "input_cost_per_token": 0.28 / 1_000_000,
        "output_cost_per_token": 0.42 / 1_000_000,
        "input_cost_per_token_cache_hit": 0.028 / 1_000_000,
    },
}


def test_known_model_cost_computed_in_cents() -> None:
    """已知模型：按价表计算并归一到美分。"""
    # 1000 input（无缓存命中）+ 500 output
    usage = UsageBreakdown(input_tokens=1000, output_tokens=500, cache_hit_tokens=0)
    with patch("litellm.model_cost", _FAKE_MODEL_COST):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")

    expected_cents = (1000 * (0.28 / 1e6) + 500 * (0.42 / 1e6)) * 100
    assert cents is not None
    assert cents == pytest.approx(expected_cents)


def test_cache_hit_tokens_billed_at_discount() -> None:
    """缓存命中 token 按折扣价（cache_hit 不计入全价输入）。"""
    usage = UsageBreakdown(input_tokens=1000, output_tokens=100, cache_hit_tokens=900)
    with patch("litellm.model_cost", _FAKE_MODEL_COST):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")

    assert cents is not None
    # billed_input = 1000 - 900 = 100
    expected_cents = (
        900 * (0.028 / 1e6) + 100 * (0.28 / 1e6) + 100 * (0.42 / 1e6)
    ) * 100
    assert cents == pytest.approx(expected_cents)


def test_reasoning_tokens_counted_as_output() -> None:
    """reasoning_tokens 并入 output 计费（输出侧）。"""
    usage = UsageBreakdown(input_tokens=500, output_tokens=500, cache_hit_tokens=0)
    with patch("litellm.model_cost", _FAKE_MODEL_COST):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")

    assert cents is not None
    # output_tokens 已含 reasoning；直接按 output 计
    expected_cents = (500 * (0.28 / 1e6) + 500 * (0.42 / 1e6)) * 100
    assert cents == pytest.approx(expected_cents)


def test_unknown_model_returns_none() -> None:
    """未知模型返回 None（不估算）。"""
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    with patch("litellm.model_cost", _FAKE_MODEL_COST):
        assert estimate_cost(usage, "deepseek/not-in-table") is None


def test_missing_price_fields_returns_none() -> None:
    """价格字段缺失回退 None（不抛）。"""
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    cost_table = {"deepseek/x": {"input_cost_per_token": 1e-6}}
    with patch("litellm.model_cost", cost_table):
        assert estimate_cost(usage, "deepseek/x") is None


def test_catalog_query_exception_returns_none() -> None:
    """litellm.model_cost 查询异常回退 None（不抛）。"""
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    with patch("litellm.model_cost", side_effect=RuntimeError("down")):
        assert estimate_cost(usage, "deepseek/deepseek-chat") is None
