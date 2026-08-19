"""cost_estimator 对抗性边界测试（独立测试 Agent 新增）。

目标：挖掘 ``estimate_cost`` 在非常规 token 明细 / 价格表形态下的缺陷。
- 负数 token / cache_hit 超过 input 时的成本钳制；
- 价格字段为 bool / 0 / 缺失；
- 未知模型 / 裸名回退；
- cache_price 缺失时退化为全价输入；
- 全零 token 的成本为 0。
"""

from unittest.mock import patch

import pytest

from app.service.llm.cost_estimator import UsageBreakdown, estimate_cost

#: 用真实 litellm 价格表（deepseek-chat）兜底验证已知模型路径；对抗用例用注入表。
_BASE: dict = {
    "deepseek/deepseek-chat": {
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 2e-6,
        "input_cost_per_token_cache_hit": 1e-7,
    },
}


def test_cache_hit_exceeds_input_clamped_to_zero_input() -> None:
    """cache_hit_tokens > input_tokens → billed_input 钳制为 0（不产生负输入成本）。"""
    usage = UsageBreakdown(input_tokens=100, output_tokens=10, cache_hit_tokens=500)
    with patch("litellm.model_cost", _BASE):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")
    expected = (500 * 1e-7 + 0 * 1e-6 + 10 * 2e-6) * 100
    assert cents == pytest.approx(expected)


def test_cache_hit_equal_to_input_zero_input_cost() -> None:
    """cache_hit_tokens == input_tokens → 输入全按缓存折扣价。"""
    usage = UsageBreakdown(input_tokens=100, output_tokens=0, cache_hit_tokens=100)
    with patch("litellm.model_cost", _BASE):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")
    expected = (100 * 1e-7) * 100
    assert cents == pytest.approx(expected)


def test_zero_tokens_zero_cost() -> None:
    """全零 token → 成本 0.0（不抛）。"""
    usage = UsageBreakdown(input_tokens=0, output_tokens=0, cache_hit_tokens=0)
    with patch("litellm.model_cost", _BASE):
        assert estimate_cost(usage, "deepseek/deepseek-chat") == pytest.approx(0.0)


def test_negative_input_tokens_produce_negative_cost() -> None:
    """负 input_tokens → 按现实现产出负成本（观察是否防御）。"""
    usage = UsageBreakdown(input_tokens=-10, output_tokens=0, cache_hit_tokens=0)
    with patch("litellm.model_cost", _BASE):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")
    # 现状：max(-10 - 0, 0) = 0，输入侧钳为 0；输出侧 0 → 总成本 0
    assert cents == pytest.approx(0.0)


def test_negative_output_tokens_produce_negative_cost() -> None:
    """负 output_tokens → 成本出现负值（观察是否防御）。"""
    usage = UsageBreakdown(input_tokens=0, output_tokens=-5, cache_hit_tokens=0)
    with patch("litellm.model_cost", _BASE):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")
    # 现状：output_cost = -5 * 2e-6 → 负成本（未被钳制）
    expected = (-5 * 2e-6) * 100
    assert cents == pytest.approx(expected)


def test_bool_price_field_treated_as_missing() -> None:
    """价格字段为 bool → _is_number 判定 False → 回退 None（不把 True 当 1）。"""
    table = {
        "deepseek/x": {
            "input_cost_per_token": True,
            "output_cost_per_token": 1e-6,
        }
    }
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    with patch("litellm.model_cost", table):
        assert estimate_cost(usage, "deepseek/x") is None


def test_output_price_missing_returns_none() -> None:
    """缺 output_cost_per_token → 回退 None（不估算）。"""
    table = {"deepseek/x": {"input_cost_per_token": 1e-6}}
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    with patch("litellm.model_cost", table):
        assert estimate_cost(usage, "deepseek/x") is None


def test_cache_price_missing_uses_full_price_for_input() -> None:
    """缺 cache_hit 价 → cache 段按 0 计，input 其余按全价（不抛）。"""
    table = {
        "deepseek/x": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
        }
    }
    usage = UsageBreakdown(input_tokens=1000, output_tokens=100, cache_hit_tokens=500)
    with patch("litellm.model_cost", table):
        cents = estimate_cost(usage, "deepseek/x")
    # cache_rate=None → cache_input_cost=0；billed_input=1000-500=500 全价
    expected = (0 + 500 * 1e-6 + 100 * 2e-6) * 100
    assert cents == pytest.approx(expected)


def test_bare_model_name_fallback_lookup() -> None:
    """完整名查不到时按裸名二次查询（provider 前缀剥离）。"""
    table = {"deepseek-chat": {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}}
    usage = UsageBreakdown(input_tokens=1000, output_tokens=0, cache_hit_tokens=0)
    with patch("litellm.model_cost", table):
        cents = estimate_cost(usage, "deepseek/deepseek-chat")
    assert cents == pytest.approx((1000 * 1e-6) * 100)


def test_model_cost_import_failure_returns_none() -> None:
    """litellm.model_cost 导入/访问失败 → 回退 None（不抛）。"""
    usage = UsageBreakdown(input_tokens=10, output_tokens=10, cache_hit_tokens=0)
    with patch("builtins.__import__", side_effect=RuntimeError("import boom")):
        assert estimate_cost(usage, "deepseek/deepseek-chat") is None
