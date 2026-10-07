"""视觉输入相关异常（纯异常定义，无副作用）。

单一职责：定义多模态视觉输入链路的专用异常类型。这些异常均为 ``ValueError`` 子类，
便于上层按"用户输入/请求参数错误"语义统一捕获并转中文引导，而不与内部系统错误混淆。

异常分工：
- ``VisionImageError``：canonical 图片引用被写坏（``file_id`` 缺失或非非空字符串）时由
  ``resolve_messages_for_model`` 抛出。附件不可用、体积超限等由附件层抛
  ``ImageNormalizationError``，两者目前都直接冒到 run 失败收敛路径，**没有**统一捕获点。
- ``VisionNotSupportedError``：模型/厂商当前不支持视觉输入。设计上由 API 层捕获并转
  HTTP 4xx + 中文引导；**当前运行期没有任何抛出点**（``create_run`` 的图片能力校验用的是
  裸 ``ValueError``），保留类型以待接入。
- ``VisionFormatNotSupportedError``：用户所选厂商协议的视觉格式本期未实现（如非
  ``openai_url`` 的 Anthropic / Gemini 形式）。**当前运行期没有抛出点**（视觉格式闸门尚未
  落地），保留类型以待接入。
"""

from __future__ import annotations


class VisionNotSupportedError(ValueError):
    """模型或厂商当前不支持视觉输入。

    设计用途：``create_run`` 构建期校验到模型不支持图片输入、或运行期转抛的视觉格式/体积错误
    统一归一到此类型，由 API 层捕获为 HTTP 4xx + 中文引导。**当前两类场景都尚未接入**
    （构建期用的是裸 ``ValueError``），保留类型以待接入。
    """


class VisionFormatNotSupportedError(ValueError):
    """用户所选厂商协议的视觉输入格式本期未实现。

    本期仅实现 ``openai_url``（DeepSeek 等 OpenAI 兼容厂商）；其它厂商的视觉格式
    （Anthropic images / Gemini inline）尚未落地。**当前运行期没有抛出点**（视觉格式闸门
    未实现），保留类型以待接入。
    """


class VisionImageError(ValueError):
    """canonical 图片引用被写坏，属整轮不可恢复的输入错误。

    由 ``resolve_messages_for_model`` 在 image 块的 ``file_id`` 缺失或不是非空字符串时抛出。
    附件不存在/越界/体积超限由附件层抛 ``ImageNormalizationError``；两者当前都没有统一捕获点，
    会直接冒到 run 失败收敛路径。
    """
