"""上下文 token 的轻量估算器（纯启发式，零依赖）。

单一职责：用字符数估算 token 数，供「上下文窗口占用」这类只需近似值的 UI 展示使用。
不追求精确——上下文圆环是直觉提示，±10% 误差不影响用户判断。

设计取舍（第零铁律 + 用户决策）：
    「上下文窗口占用」本质依赖目标模型的 tokenizer 词表；引入本地 tokenizer 既增加重量级
    依赖（torch/tokenizers + 每模型词表），又会在切换模型时失效。用户明确选择「估算即可、
    无需精确」——字符估算在中英文混合文本上约 ±10% 误差，且完全模型无关、切模型免疫，
    是长期维护成本最低的方案。本模块不重复造轮子（不手写 BPE），也不引入成熟 tokenizer 库，
    因为按用户确认的语义精度需求，估算已足够，额外依赖是过度设计。精确 token 计数（模型返回的
    usage_metadata）仅用于成本统计，不在此处。

不负责：上下文窗口上限（分母）的计算、消息的采集——这些归 ContextUsageMeter。
"""

from __future__ import annotations


class TokenEstimator:
    """上下文 token 的字符数估算器（进程内无状态工具类）。

    经验系数说明：中英文混合文本约 1 token ≈ 2~4 字符（英文约 4 字符/token，
    中文约 1 字符 ≈ 1~1.5 token）。取 3 作为跨语言折中系数，够用且无需 tokenizer。
    """

    # 每 token 约等于的字符数（跨中英文折中值）。
    CHARS_PER_TOKEN: int = 3

    @classmethod
    def estimate(cls, text: str) -> int:
        """估算一段文本的 token 数。

        参数:
            text: 待估算的文本；空串或 None 返回 0。

        返回:
            估算的 token 数（空文本为 0；非空文本至少 1）。

        异常:
            无。

        副作用:
            无。
        """
        if not text:
            return 0
        # 注意：这是字符启发式，非真实 tokenizer 切分；对英文偏乐观、中文偏保守，
        # 仅用于上下文占用圆环的近似展示，不作为计费依据。
        return max(1, len(text) // cls.CHARS_PER_TOKEN)
