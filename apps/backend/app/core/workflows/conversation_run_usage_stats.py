"""一次 turn 的 token 与耗时统计（运行时累加器）。

本值对象作为 ``RuntimeConfig`` 的可变成员被 model 节点写入、被 ``run_finished`` 事件读取；
不进入 LangGraph checkpoint，只在单次 graph 执行期间生效。
"""

import math
from dataclasses import dataclass
from typing import Any

# LangChain UsageMetadata 契约中缓存/推理细节的键名（实测自 langchain_litellm 的
# _create_usage_metadata：usage_metadata["input_token_details"]["cache_read"]、
# usage_metadata["output_token_details"]["reasoning"]）。旧 OpenAI 兼容扁平键
# prompt_cache_hit_tokens / reasoning_tokens 在 litellm 链路下不会被填充，勿再用。
_INPUT_CACHE_READ_KEY = "cache_read"
_OUTPUT_REASONING_KEY = "reasoning"


@dataclass
class ConversationRunUsageStats:
    """run 级 token 与耗时累加器。

    字段语义与 LangChain ``UsageMetadata`` 对齐（见 langchain_core.messages.ai）：
    - ``input_tokens``：输入 token 数（含历史、工具结果等）。
    - ``output_tokens``：模型生成 token 数。
    - ``total_tokens``：总 token 数（输入 + 输出）。
    - ``cache_hit_tokens``：缓存命中 token 数（usage_metadata.input_token_details.cache_read）。
    - ``cache_miss_tokens``：缓存未命中 token 数；当模型仅返回 total 时，可用
      ``total - cache_hit`` 估算。
    - ``reasoning_tokens``：推理模型产生的思考 token 数
      （usage_metadata.output_token_details.reasoning）。

    Attributes:
        input_tokens: 累计输入 token 数。
        output_tokens: 累计输出 token 数。
        total_tokens: 累计总 token 数。
        cache_hit_tokens: 累计缓存命中 token 数。
        cache_miss_tokens: 累计缓存未命中 token 数。
        reasoning_tokens: 累计推理 token 数。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int | None = None
    reasoning_tokens: int = 0
    @staticmethod
    def _safe_int(value: Any) -> int:
        """把非负有限整数安全转为 int；异常值返回 0。"""
        if isinstance(value, bool) or value is None:
            return 0
        if isinstance(value, float) and (
            not math.isfinite(value) or not value.is_integer() or value < 0
        ):
            return 0
        try:
            converted = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return (
            converted
            if converted >= 0 and math.isfinite(numeric) and numeric == converted
            else 0
        )

    def add_usage_metadata(self, usage_metadata: dict[str, Any] | None) -> None:
        """按 LangChain ``UsageMetadata`` 标准契约累加一次模型调用的 token 统计。

        单一事实来源：模型节点在每次模型调用产出 ``ai_message`` 后调用本方法，
        从 ``ai_message.usage_metadata``（LangChain 已对各流式 chunk 求和无重复）取数，
        不再逐 chunk 解析，消除双重口径。

        参数:
            usage_metadata: ``AIMessage.usage_metadata``（dict 或 None）；None / 空字典时安全忽略。

        返回:
            无。

        异常:
            不抛出异常；字段缺失或类型异常时仅跳过该字段。

        副作用:
            就地累加本对象各字段。本对象为 turn 级共享累加器，多次模型调用（含 REPAIR 回流）
            会依次累加，不会相互覆盖。
        """
        if not usage_metadata:
            return

        self.input_tokens += self._safe_int(usage_metadata.get("input_tokens"))
        self.output_tokens += self._safe_int(usage_metadata.get("output_tokens"))
        self.total_tokens += self._safe_int(usage_metadata.get("total_tokens"))

        cache_details_complete = getattr(self, "_cache_details_complete", True)
        input_details = usage_metadata.get("input_token_details")
        if isinstance(input_details, dict) and _INPUT_CACHE_READ_KEY in input_details:
            self.cache_hit_tokens += self._safe_int(input_details.get(_INPUT_CACHE_READ_KEY))
        else:
            cache_details_complete = False
        output_details = usage_metadata.get("output_token_details") or {}
        if isinstance(output_details, dict):
            self.reasoning_tokens += self._safe_int(output_details.get(_OUTPUT_REASONING_KEY))

        # cache_read 是 input_tokens 的子集。未命中值不再保持永久 0，按累计输入减
        # 累计命中推导，并对 provider 异常的 cache_read > input 做下限保护。
        self.cache_miss_tokens = (
            max(0, self.input_tokens - self.cache_hit_tokens)
            if cache_details_complete
            else None
        )
        self._cache_details_complete = cache_details_complete

    def to_dict(self) -> dict[str, int | None]:
        """把统计转成可序列化字典。

        参数:
            无。

        返回:
            各字段均为 int 的字典，供 ``RunFinishedPayload`` 使用。
        """

        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }
