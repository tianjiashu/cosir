"""单个 Agent 的模型覆盖配置值对象。

只承载该 Agent 的模型覆盖配置（生成参数 + 推理强度 + 可选接入覆盖），不参与模型构建。
所有字段均为可选覆盖项：未提供（``None``）时，由全局运行配置（``app.config.settings``）
与 ``ProviderCapability`` 注册表提供缺省值，``ModelSettings`` 仅覆盖显式给出的字段。

``_FIELDS`` 是 ``to_dict`` / ``from_dict`` 的权威字段清单，必须与 dataclass 字段完全一致
（新增字段须同步追加），否则 JSON 往返会静默丢失字段。
"""

from typing import Any

# 序列化权威字段清单：与 ModelSettings 全部字段一一对应，新增字段须同步追加。
_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "thinking",
    "base_url",
    "api_key",
    "provider_type",
    "drop_params",
    "stream",
    "reasoning_effort",
)


class ModelSettings:
    """单个 Agent 的模型覆盖配置值对象（声明式覆盖项，不参与模型构建）。

    字段分类：
    - 采样参数：``temperature`` / ``top_p`` / ``max_tokens``；
    - 推理强度：``reasoning_effort``（``low``/``high``/``max``，None 时不注入）；
    - thinking 开关：``thinking``（是否抽取/回传思考块）；
    - 接入与能力覆盖：``provider_type``（注册表键，None 时按 model_name 推导）/
      ``drop_params``（None 时回退 ``ProviderCapability.default_drop_params``）/
      ``stream``（None 时不覆盖）/ ``response_format``（text / json_object，None 时不注入）。

    全局默认值（超时/重试/seed 等）归 ``app.config.settings`` 与 ``ProviderCapability``
    注册表；上下文窗口上限归 ``ModelCatalog`` + ``Settings.CONTEXT_WINDOW_TOKENS``（见
    ``context_window_resolver``），此处不为假想需求预留覆盖字段。

    注：``_FIELDS`` 与实际 dataclass 字段存在漂移（缺 ``response_format``、多 ``base_url``/
    ``api_key``），序列化契约当前不严谨，需后续修正（不在本次注释精简范围）。
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool = True
    # 可选厂商类型（注册表键，如 ``deepseek`` / ``azure``）：None 时由
    # ``factory.build_chat_model`` 按 model_name 前缀回退推导。
    provider_type: str | None = None
    # 可选是否丢弃不支持参数覆盖：None 时回退 ``ProviderCapability.default_drop_params``。
    drop_params: bool | None = None
    # 可选流式开关覆盖：None 时不覆盖（沿用运行时默认流式）。
    stream: bool = True
    # 可选推理强度覆盖（``low`` / ``high`` / ``max``）：None 时不注入。
    reasoning_effort: str = "max"
    # 可选响应格式覆盖：None 时不注入。text / json_object
    response_format: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可序列化字典（仅含非 ``None`` 字段，字段范围由 ``_FIELDS`` 决定）。"""

        return {k: getattr(self, k) for k in _FIELDS if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSettings":
        """从字典重建，忽略未知键（前向兼容，缺失键按默认值 None 处理）。"""

        return cls(**{k: data[k] for k in _FIELDS if k in data})
