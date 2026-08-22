"""模型调用成本估算服务（设计文档阶段 5 ②）。

单一职责：按 litellm 价格表（``litellm.model_cost``）估算一次模型调用（turn 级
usage）的美元成本，归一为美分（cents）。已知模型按价表计算输入 / 输出 / 缓存命中
三段成本；未知模型返回 None（不估算，避免误导）；目录查询失败静默回退 None（不抛，
避免拖垮统计链路）。

不负责：token 统计的累加（归 ``TurnUsageStats``）、错误码归一（归
``model_error_mapper``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.logging.logger import log


@dataclass(frozen=True)
class UsageBreakdown:
    """一次模型调用的 token 明细（成本估算的输入）。

    属性:
        input_tokens: 输入 token 数（不含缓存命中）。
        output_tokens: 输出 token 数（含 reasoning tokens）。
        cache_hit_tokens: 缓存命中 token 数（按折扣价计）。
    """

    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int


def estimate_cost(usage: UsageBreakdown, model_name: str) -> float | None:
    """按 litellm 价格表估算模型调用成本（美分）。

    查表用完整 ``model_name``（带 provider 前缀，litellm 价格表键格式）；查不到
    时尝试裸名（``rsplit("/", 1)[-1]``）二次查询。成本公式：
    ``cache_hit * input_cache_price + (input - cache_hit) * input_price + output * output_price``，
    输出侧把 reasoning tokens 并入 output 计费。未知模型 / 价格字段缺失 / 查询异常
    均返回 None（不抛）。

    参数:
        usage: token 明细（``UsageBreakdown``）。
        model_name: 完整模型名（带 provider 前缀，如 ``deepseek/deepseek-v4-flash``）。

    返回:
        估算成本（美分，浮点）；未知模型或查询失败返回 None。

    异常:
        无（litellm 价格表异常在此捕获并降级为 None）。

    副作用:
        查询失败写一次 warn 级 ``cost_estimate_failed`` 日志（带模型名）。
    """

    price = _lookup_price(model_name)
    if price is None:
        return None
    cache_price = price.get("input_cost_per_token_cache_hit")
    input_price = price.get("input_cost_per_token")
    output_price = price.get("output_cost_per_token")
    if not _is_number(input_price) or not _is_number(output_price):
        log.warning(
            "cost_estimate_failed",
            extra={
                "msg": "model cost entry missing price fields; skipping cost estimate",
                "data": {"model": model_name},
            },
        )
        return None
    # _is_number 已收窄为 int|float（非 bool）；此处经显式判空转为 float。
    cache_rate = _as_float(cache_price)
    cache_input_cost = usage.cache_hit_tokens * cache_rate if cache_rate is not None else 0.0
    billed_input = max(usage.input_tokens - usage.cache_hit_tokens, 0)
    input_cost = billed_input * float(input_price)  # type: ignore[arg-type]  # 已 _is_number 收窄
    output_cost = usage.output_tokens * float(output_price)  # type: ignore[arg-type]  # 已 _is_number 收窄
    dollars = cache_input_cost + input_cost + output_cost
    return dollars * 100.0


def _lookup_price(model_name: str) -> dict | None:
    """从 litellm 价格表查找模型单价（完整名优先，裸名回退）。

    参数:
        model_name: 完整模型名（带 provider 前缀）。

    返回:
        litellm 价格表条目（dict）；未收录或查询异常返回 None。

    异常:
        无（litellm 查询异常在此捕获并降级）。

    副作用:
        无（纯字典查找；litellm 目录为进程内静态表）。
    """
    try:
        from litellm import model_cost
    except Exception:
        return None
    if not isinstance(model_cost, dict):
        return None
    entry = model_cost.get(model_name)
    if entry is None:
        bare = model_name.rsplit("/", 1)[-1]
        entry = model_cost.get(bare)
    return entry if isinstance(entry, dict) else None


def _is_number(value) -> bool:
    """判断值是否为可参与浮点运算的数字（不含 bool）。

    参数:
        value: 待判定的值。

    返回:
        非 bool 且可转为 float 时返回 True。

    异常:
        无。

    副作用:
        无。
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_float(value) -> float | None:
    """把已判定为数字的值转为 float；非数字返回 None。

    参数:
        value: 待转换的值（可能为 int/float/None/任意类型）。

    返回:
        数字值的 float 形式；非数字或 None 返回 None。

    异常:
        无。

    副作用:
        无。
    """
    if _is_number(value):
        return float(value)
    return None
