"""一次 turn 的 token 与耗时统计（运行时累加器）。

本值对象作为 ``RuntimeConfig`` 的可变成员被 model 节点写入、被 ``run_finished`` 事件读取；
不进入 LangGraph checkpoint，只在单次 graph 执行期间生效。
"""

from dataclasses import dataclass


@dataclass
class TurnUsageStats:
    """turn 级 token 与耗时累加器。

    字段语义与 LangChain / OpenAI usage_metadata 对齐：
    - ``input_tokens``：输入 token 数（含历史、工具结果等）。
    - ``output_tokens``：模型生成 token 数。
    - ``total_tokens``：总 token 数（输入 + 输出）。
    - ``cache_hit_tokens``：缓存命中 token 数（DeepSeek 等模型可能返回）。
    - ``cache_miss_tokens``：缓存未命中 token 数；当模型仅返回 total 时，可用
      ``total - cache_hit`` 估算。
    - ``reasoning_tokens``：推理模型产生的思考 token 数（如 DeepSeek 的 reasoning）。

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
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0

    def add_message_usage(self, usage_metadata: dict[str, int | float] | None) -> None:
        """把一条 LangChain 消息的 usage_metadata 累加到当前统计。

        参数:
            usage_metadata: LangChain 消息附带的 usage 元数据；不同 provider 字段不同，
                缺失时安全忽略。

        返回:
            无。

        异常:
            不抛出异常；字段类型异常时仅跳过该字段。

        副作用:
            就地累加本对象各字段。
        """

        if not usage_metadata:
            return

        def _int(value) -> int:
            """把字段值安全转为 int；无法转换时返回 0。"""
            if isinstance(value, bool):
                return 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        # LangChain/OpenAI 标准字段
        self.input_tokens += _int(usage_metadata.get("input_tokens"))
        self.output_tokens += _int(usage_metadata.get("output_tokens"))
        self.total_tokens += _int(usage_metadata.get("total_tokens"))
        # DeepSeek/OpenAI 缓存相关字段（不同 SDK 命名可能不同）
        self.cache_hit_tokens += _int(usage_metadata.get("prompt_cache_hit_tokens"))
        self.cache_miss_tokens += _int(usage_metadata.get("prompt_cache_miss_tokens"))
        # 推理模型思考 token
        self.reasoning_tokens += _int(usage_metadata.get("reasoning_tokens"))

    def to_dict(self) -> dict[str, int]:
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
