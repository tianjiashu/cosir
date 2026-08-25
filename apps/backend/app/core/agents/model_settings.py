"""单个 Agent 的模型覆盖配置值对象。

单一职责：只承载该 Agent 的模型覆盖配置（生成参数 + thinking 模式 + 可选接入覆盖），
不参与模型构建。所有字段均为可选覆盖项：未提供（``None``）时，由全局运行配置
（``app.config.settings`` 模块级静态变量）与 ``ProviderCapability`` 注册表提供缺省值，
``ModelSettings`` 仅覆盖显式给出的字段。

序列化契约：``_FIELDS`` 是 ``to_dict`` / ``from_dict`` 的**权威字段清单**，必须与
下方 dataclass 字段保持完全一致（新增字段须同步追加），否则会在 JSON 往返中静默丢失。
"""

from dataclasses import dataclass
from typing import Any

# 序列化权威字段清单：与 ModelSettings 全部字段一一对应。
# 新增字段时务必同步追加，否则 to_dict/from_dict 会静默丢弃该字段。
_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "thinking",
    "base_url",
    "api_key",
    "provider_type",
    "thinking_channels",
    "thinking_roundtrip",
    "thinking_display",
    "max_retries",
    "drop_params",
    "timeout_seconds",
    "stream",
    "reasoning_effort",
)


class ModelSettings:
    """单个 Agent 的模型覆盖配置值对象。

    职责边界：
    - 负责：声明 Agent 级别的模型覆盖项。分为三类——
      1) 采样参数：``temperature`` / ``top_p`` / ``max_tokens``；
      2) thinking 生命周期：``thinking``（是否开启）/ ``thinking_channels``
         （响应侧抽取通道）/ ``thinking_roundtrip``（是否原样回传 thinking 块）/
         ``thinking_display``（展示模式 full/summary/off）/
         ``reasoning_effort``（low/high/max）；
      3) 接入与能力覆盖：``base_url``（可选 api_base）/ ``api_key``（明文覆盖）/
         ``provider_type``（注册表键）/ ``max_retries`` / ``drop_params`` /
         ``timeout_seconds`` / ``stream``。
    - 不负责：模型构建（交给 ``core.llm.factory.build_chat_model``）、全局默认值
      （交给 ``app.config.settings`` 与 ``ProviderCapability`` 注册表）、字段校验
      语义（仅做「是否提供」的覆盖判断）。

    说明:
        上下文窗口上限不在此处（亦无 Agent 级覆盖需求）：模型最大窗口归 ``ModelCatalog``，
        全局软上限归 ``Settings.CONTEXT_WINDOW_TOKENS``，两者 min 即实际上限
        （见 ``context_window_resolver.resolve_context_window``）。不为假想需求预留覆盖字段。
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool = True
    # 可选厂商类型（注册表键，如 ``deepseek`` / ``azure``）：None 时由
    # ``factory.build_chat_model`` 按 model_name 前缀回退推导。
    provider_type: str | None = None
    # 可选最大重试次数覆盖：None 时回退 ``ProviderCapability.default_max_retries``。
    max_retries: int | None = None
    # 可选是否丢弃不支持参数覆盖：None 时回退 ``ProviderCapability.default_drop_params``。
    drop_params: bool | None = None
    # 可选请求超时秒数覆盖：None 时回退 ``ProviderCapability.default_timeout_seconds``。
    timeout_seconds: float | None = None
    # 可选流式开关覆盖：None 时不覆盖（沿用运行时默认流式）。
    stream: bool = True
    # 可选推理强度覆盖（``low`` / ``high`` / ``max``）：None 时不注入。
    reasoning_effort: str | None = None
    # 可选响应格式覆盖：None 时不注入。text / json_object
    response_format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可序列化字典（仅含非 ``None`` 字段）。

        字段范围由 ``_FIELDS`` 权威决定，必须与 dataclass 字段完全一致，
        否则 JSON 往返会静默丢失字段。

        参数:
            无。

        返回:
            只包含显式覆盖字段（非 ``None``）的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {k: getattr(self, k) for k in _FIELDS if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSettings":
        """从字典重建，忽略未知键（前向兼容）。

        字段范围由 ``_FIELDS`` 决定，未知键（未来版本新增或历史残留）直接忽略，
        避免反序列化失败。

        参数:
            data: 原始字典（可能含未来版本新增字段或历史残留键）。

        返回:
            重建的 ``ModelSettings`` 实例。

        异常:
            无（忽略未知键，缺失键按默认值 None 处理）。

        副作用:
            无。
        """

        return cls(**{k: data[k] for k in _FIELDS if k in data})
