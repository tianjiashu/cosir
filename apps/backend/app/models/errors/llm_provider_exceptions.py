"""视觉输入相关异常（纯异常定义，无副作用）。

单一职责：定义多模态视觉输入链路的专用异常类型。这些异常均为 ``ValueError`` 子类，
便于上层按"用户输入/请求参数错误"语义统一捕获并转中文引导，而不与内部系统错误混淆。

异常分工：
- ``VisionNotSupportedError``：模型/厂商当前不支持视觉输入（构建期 ``create_turn`` 或
  运行期 workflow 转抛），由 API 层捕获并转 HTTP 4xx + 中文引导。
- ``VisionFormatNotSupportedError``：用户所选厂商协议的视觉格式本期未实现（如非
  ``openai_url`` 的 Anthropic / Gemini 形式），由 workflow 捕获后转 ``VisionNotSupportedError``。
- ``VisionImageError``：单轮聚合体积超限等运行期硬错（非逐图软失败），由 workflow 捕获后
  转 ``VisionNotSupportedError`` 统一中文引导。
"""

from __future__ import annotations


class VisionNotSupportedError(ValueError):
    """模型或厂商当前不支持视觉输入。

    触发场景：``create_turn`` 构建期校验到 ``ModelCapability.supports_image`` 为 False；
    或运行期 workflow 转抛的「厂商视觉格式未实现 / 聚合体积超限」等，统一归一到此类型，
    由 API 层捕获为 HTTP 4xx + 中文引导。
    """


class VisionFormatNotSupportedError(ValueError):
    """用户所选厂商协议的视觉输入格式本期未实现。

    本期仅实现 ``openai_url``（DeepSeek 等 OpenAI 兼容厂商）。其它厂商的视觉格式
    （Anthropic images / Gemini inline）尚未落地，由 ``build_user_content_blocks`` 抛出，
    workflow 捕获后转 ``VisionNotSupportedError``，避免运行期裸 ``NotImplementedError``。
    """


class VisionImageError(ValueError):
    """单轮视觉输入聚合体积超限等运行期硬错。

    与逐图软失败（``skipped`` 列表）不同：本异常代表整轮不可恢复（如全部图片体积累加
    超过 48MiB 上限），由 ``build_user_content_blocks`` 抛出，workflow 捕获后转
    ``VisionNotSupportedError`` 统一中文引导。
    """
