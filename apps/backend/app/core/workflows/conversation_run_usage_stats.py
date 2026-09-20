"""一次 run 的 token 与耗时统计（运行时累加器）。

本值对象作为 ``RuntimeConfig`` 的可变成员被 model 节点写入、被 Run 终态事件读取；
不进入 LangGraph checkpoint，只在单次 graph 执行期间生效。
"""
import math
from dataclasses import dataclass
from typing import Any

from app.config.constant import Constant

# LangChain UsageMetadata 契约中缓存/推理细节的键名（以 provider 返回的
# _create_usage_metadata：usage_metadata["input_token_details"]["cache_read"]、
# usage_metadata["output_token_details"]["reasoning"]）。旧 OpenAI 兼容扁平键
# prompt_cache_hit_tokens / reasoning_tokens 等旧版扁平键不再作为兼容分支，勿再用。


@dataclass
class ConversationRunUsageStats:
    """run 级 token 与耗时快照（覆盖更新）。

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
        input_tokens: 最近一次模型调用覆盖后的输入 token 数。
        output_tokens: 最近一次模型调用覆盖后的输出 token 数。
        total_tokens: 最近一次模型调用覆盖后的总 token 数。
        cache_hit_tokens: 最近一次模型调用覆盖后的缓存命中 token 数。
        cache_miss_tokens: 最近一次模型调用覆盖后的缓存未命中 token 数。
        reasoning_tokens: 最近一次模型调用覆盖后的推理 token 数。

    另有一个**非 dataclass 字段**的实例状态 ``_cache_details_complete``：记录最近一次模型调用是否
    给出了缓存明细。它只用于决定 ``cache_miss_tokens`` 能否由本次值推导，因此不进字段表、
    也不参与 ``to_dict``。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int | None = None
    reasoning_tokens: int = 0

    @staticmethod
    def _safe_int(value: Any) -> int:
        """把非负有限整数安全转为 int；异常值返回 0。

        参数:
            value: provider ``usage_metadata`` 中的原始值。

        返回:
            非负整数；``bool`` / ``None`` / 非数值 / 负值 / 非有限浮点 / 含小数浮点均返回 0。

        异常:
            无（内部捕获 ``TypeError`` / ``ValueError`` / ``OverflowError``）。

        副作用:
            无。
        """
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
        """按 LangChain ``UsageMetadata`` 标准契约覆盖更新一次模型调用的 token 统计。

        单一事实来源：模型节点在每次模型调用产出 ``ai_message`` 后调用本方法，
        从 ``ai_message.usage_metadata`` 取数。实测表明 provider 返回的
        ``usage_metadata`` 已是该次 run 的累计值，因此本方法采用「覆盖更新」语义：
        每次调用都用最新一次的用量**替换**既有值，而非逐次相加，避免多轮 / 多子调用
        回流时重复累加导致总量虚高。最终生效的是最后一次有效 ``usage_metadata`` 的快照。

        参数:
            usage_metadata: ``AIMessage.usage_metadata``（dict 或 None）；None / 空字典时安全忽略。

        返回:
            无。

        异常:
            不抛出异常；字段缺失或类型异常时仅跳过该字段。

        副作用:
            就地用最新一次用量覆盖本对象各字段。本对象是 run 级共享状态，以最后一次
            有效 ``usage_metadata`` 的快照为准，不跨调用累加。
        """
        if not usage_metadata:
            return

        self.input_tokens = self._safe_int(usage_metadata.get("input_tokens"))
        self.output_tokens = self._safe_int(usage_metadata.get("output_tokens"))
        self.total_tokens = self._safe_int(usage_metadata.get("total_tokens"))

        cache_details_complete = False
        cache_hit = 0
        input_details = usage_metadata.get("input_token_details")
        if isinstance(input_details, dict) and Constant.Workflow.INPUT_CACHE_READ_KEY in input_details:
            cache_hit = self._safe_int(input_details.get(Constant.Workflow.INPUT_CACHE_READ_KEY))
            cache_details_complete = True
        self.cache_hit_tokens = cache_hit

        output_details = usage_metadata.get("output_token_details") or {}
        if isinstance(output_details, dict):
            self.reasoning_tokens = self._safe_int(output_details.get(Constant.Workflow.OUTPUT_REASONING_KEY))
        else:
            self.reasoning_tokens = 0

        # cache_read 是 input_tokens 的子集。未命中值仅在本次 provider 给出缓存明细时
        # 按 input - cache_hit 推导，并对 cache_read > input 做下限保护；否则置 None。
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
            六字段字典，供 ``RunFinishedPayload`` 使用；``cache_miss_tokens`` 在 provider 从未
            返回缓存明细时为 ``None``，其余字段均为 int。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }
