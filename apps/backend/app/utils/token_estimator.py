"""上下文 token 的轻量估算器（纯启发式，零依赖）。

单一职责：用字符数估算 token 数，供「上下文窗口占用」这类只需近似值的 UI 展示使用。
不追求精确——上下文圆环是直觉提示，误差不影响用户判断。

设计取舍（第零铁律 + 用户决策）：
    「上下文窗口占用」本质依赖目标模型的 tokenizer 词表；引入本地 tokenizer 既增加重量级
    依赖（torch/tokenizers + 每模型词表），又会在切换模型时失效。用户明确选择「估算即可、
    无需精确」。本模块按字符类别加权（中文 vs 英文）以缩小跨语言误差，仍保持纯启发式、
    模型无关、切模型免疫，是长期维护成本最低的方案。本模块不重复造轮子（不手写 BPE），
    也不引入成熟 tokenizer 库，因为按用户确认的语义精度需求，估算已足够，额外依赖是过度
    设计。精确 token 计数（模型返回的 usage_metadata）仅用于成本统计，不在此处。

若未来确实需要更高精度（比如要接近 o200k_base 的真实计数），
建议升级方向是：参考 tokenx 的语言配置表思路，把你现有的 _is_cjk 扩展为多语种正则分段 + 各自 ratio，仍保持零依赖启发式

不负责：上下文窗口上限（分母）的计算、消息的采集与角色/元数据处理——这些归
``ContextUsageMeter`` 与消息值对象。
"""

from __future__ import annotations


class TokenEstimator:
    """上下文 token 的字符类别加权估算器（进程内无状态工具类）。

    经验系数说明：英文约 4 字符 ≈ 1 token；中文（CJK）约 1 字符 ≈ 1 token。
    按字符类别分别计 token 比「统一折中系数 3」在中英文混合文本上误差更小
    （中文不再被低估、英文不再被高估），仍无需 tokenizer。
    """

    # 每 token 约等于的英文字符数（含数字、半角符号、空白）。
    ASCII_CHARS_PER_TOKEN: int = 4
    # 每 token 约等于的中文字符数（CJK 统一表意文字、扩展区、全角标点/形式）。
    CJK_CHARS_PER_TOKEN: int = 1

    @classmethod
    def estimate(cls, text: str) -> int:
        """估算一段文本的 token 数（按字符类别加权）。

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
        cjk = sum(1 for ch in text if cls._is_cjk(ch))
        other = len(text) - cjk
        tokens = cjk // cls.CJK_CHARS_PER_TOKEN + other // cls.ASCII_CHARS_PER_TOKEN
        # 注意：这是字符启发式，非真实 tokenizer 切分；仅用于上下文占用圆环的近似展示，
        # 不作为计费依据。非空文本至少计 1 token（即使极短纯英文）。
        return max(1, tokens)

    @staticmethod
    def _is_cjk(ch: str) -> bool:
        """判断单个字符是否属于中日韩（CJK）相关区间（含中文常用字符与全角标点）。

        参数:
            ch: 单个字符。

        返回:
            该字符是否按中文口径计 token。
        """
        code = ord(ch)
        return (
            (0x4E00 <= code <= 0x9FFF)  # CJK 统一表意文字（常用汉字）
            or (0x3400 <= code <= 0x4DBF)  # CJK 扩展 A
            or (0x3000 <= code <= 0x303F)  # CJK 全角标点 / 符号
            or (0xFF00 <= code <= 0xFFEF)  # 全角形式（全角标点、全角字母）
        )
