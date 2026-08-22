"""模型厂商能力静态注册表（单一事实源）。

单一职责：承载各厂商接入 litellm 所需的「能力元数据」（前缀、端点、Key 要求、
thinking 通道、自动发现端点等），供 ``api`` / ``service`` / ``core`` / ``tools``
任何层引用，避免厂商差异散落成 if-else 分支。**不负责**：厂商连通性验证（由
``provider_connection_test_service`` 在用户主动测试时进行）、模型清单实际拉取
（由 ``provider_discover_service`` 在 discover 时进行）。

放置在 ``models/`` leaf 层（不依赖 ``api`` / ``core`` / ``service`` / ``tools``），
符合分层 DAG（见 ``rules/目录组织规范.md`` 第一章）。

2026-08-18 用户决议：本地单机 SQLite 凭据明文存储不做加密；本注册表的
``requires_api_key`` 字段决定哪些厂商可不填 Key（如 ollama），与凭据加密与否无关。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ProviderCapability:
    """单个厂商接入 litellm 的能力元数据（不可变值对象）。

    参数:
        provider_type: 厂商类型字符串（与 litellm 官方术语对齐），
            如 ``deepseek`` / ``azure`` / ``zai`` / ``dashscope``。
            ``custom`` 表示任意 base_url 的自定义厂商。
        litellm_prefix: litellm 模型名前缀（含尾斜杠，如 ``deepseek/``）。
            ``None`` 表示该厂商不按前缀过滤（如 ``custom``），由用户手填完整模型名。
        default_base_url: 厂商官方默认端点模板（不含尾斜杠）。
            ``None`` 表示该厂商由 litellm 内置解析或要求用户填 base_url。
        requires_api_key: 是否要求 API Key（``ollama=False``，其余 ``True``）。
        requires_api_version: 是否要求 ``api_version`` 字段（仅 ``azure=True``）。
        requires_manual_model_entry: 是否要求用户手填模型名（厂商未提供 list 端点，
            仅 ``azure=True``——Azure OpenAI 需用户在 Azure Portal 创建 deployment
            后手填 deployment 名作为模型名）。
        discover_endpoint: 厂商模型自动发现端点模板（``{base_url}`` 占位符由
            ``provider_discover_service`` 替换为实际 base_url）。
            ``None`` 表示不可自动发现（走 ``requires_manual_model_entry`` 分支）。
        default_drop_params: 是否默认透传 ``drop_params=True`` 给 litellm
            （非严格 OpenAI 兼容厂商为 ``True``，litellm 自动丢弃不支持参数，
            避免国内厂商遇 ``thinking`` 等参数直接 400）。
        thinking_channels: 响应侧 thinking 字段通道元组（按优先级顺序尝试读取），
            可取值 ``"reasoning_content"``（DeepSeek/Kimi/OpenAI 兼容）、
            ``"thinking_blocks"``（Anthropic 备选）、``"thought"``（Gemini）、
            ``"reasoning"``（OpenAI o 系列）；空元组表示该厂商不支持 thinking。
        supports_thinking: 该厂商是否支持 thinking/reasoning 模式。
        default_timeout_seconds: 默认请求超时秒数（透传 ChatLiteLLM ``request_timeout``）。
        default_max_retries: 默认最大重试次数（透传 ChatLiteLLM ``max_retries``）。
        regions: 国内/国际双端点厂商标注（如 ``("cn", "intl")``），供 UI 提示与
            端点切换；空元组表示单一端点。
        parameter_constraints: 参数形态元数据（键值对，未定义键读不到时 UI 不提示，
            不得抛错）。当前预留：``temperature_range`` / ``max_tokens_field`` /
            ``thinking_request``，本期不实施，由「未来扩展」第 1 项落地。

    返回:
        不可变的厂商能力值对象。

    异常:
        无。

    副作用:
        无。
    """

    provider_type: str
    litellm_prefix: str | None
    default_base_url: str | None
    requires_api_key: bool
    requires_api_version: bool
    requires_manual_model_entry: bool
    discover_endpoint: str | None
    default_drop_params: bool
    thinking_channels: tuple[str, ...]
    supports_thinking: bool
    default_timeout_seconds: float
    default_max_retries: int
    regions: tuple[str, ...] = ()
    parameter_constraints: dict[str, Any] = field(default_factory=dict)


#: 默认请求超时秒数（覆盖 ChatLiteLLM 内置默认，避免国内厂商偶发慢响应超时）。
_DEFAULT_TIMEOUT_SECONDS: float = 120.0

#: 默认最大重试次数（覆盖 ChatLiteLLM 内置默认 1）。
_DEFAULT_MAX_RETRIES: int = 2

#: DeepSeek 默认 thinking 通道（reasoning_content，单通道，最稳定）。
_DEEPSEEK_CHANNELS: tuple[str, ...] = ("reasoning_content",)

#: Anthropic thinking 通道（thinking_blocks 备选；0.7.0 全包实测该字段 0 匹配，
#: 真实通道待阶段 3 开工前以本地 ``.venv`` 源码核实为准，大概率复用
#: ``reasoning_content``——litellm 已为 Anthropic 注入 thinking 块字段名
#: ``{"type":"thinking"}``）。
_ANTHROPIC_CHANNELS: tuple[str, ...] = ("thinking_blocks", "reasoning_content")

#: Gemini thinking 通道（thought parts；遍历 content 块中 ``thought=True`` 的文本）。
_GEMINI_CHANNELS: tuple[str, ...] = ("thought",)

#: OpenAI o 系列 thinking 通道（reasoning，含 encrypted_content 与 summary；
#: ⚠️ langchain_litellm 0.7.0 实测无此字段归一通路，阶段 3.2 先核实 litellm 侧处理，
#: 无通路则降级为仅摘要展示，见风险清单）。
_OPENAI_O_CHANNELS: tuple[str, ...] = ("reasoning",)


#: 厂商能力静态注册表（设计文档 §二「litellm 原生前缀实测」+ §三「厂商模型发现端点实测」
#: 的唯一事实来源，2026-08-18 已核实）。新增厂商 = 改一行；**不改任何业务代码**。
PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "deepseek": ProviderCapability(
        provider_type="deepseek",
        litellm_prefix="deepseek/",
        default_base_url="https://api.deepseek.com",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://api.deepseek.com/models",
        default_drop_params=True,
        thinking_channels=_DEEPSEEK_CHANNELS,
        supports_thinking=True,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "openai-compatible": ProviderCapability(
        provider_type="openai-compatible",
        litellm_prefix="openai/",
        default_base_url=None,
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="{base_url}/models",
        default_drop_params=False,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "anthropic": ProviderCapability(
        provider_type="anthropic",
        litellm_prefix="anthropic/",
        default_base_url=None,
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://api.anthropic.com/v1/models",
        default_drop_params=True,
        thinking_channels=_ANTHROPIC_CHANNELS,
        supports_thinking=True,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "gemini": ProviderCapability(
        provider_type="gemini",
        litellm_prefix="gemini/",
        default_base_url=None,
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint=("https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"),
        default_drop_params=True,
        thinking_channels=_GEMINI_CHANNELS,
        supports_thinking=True,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "azure": ProviderCapability(
        provider_type="azure",
        litellm_prefix="azure/",
        default_base_url=None,
        requires_api_key=True,
        requires_api_version=True,
        requires_manual_model_entry=True,
        discover_endpoint=None,
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "dashscope": ProviderCapability(
        provider_type="dashscope",
        litellm_prefix="dashscope/",
        default_base_url=("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint=("https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
        regions=("cn", "intl"),
    ),
    "moonshot": ProviderCapability(
        provider_type="moonshot",
        litellm_prefix="moonshot/",
        default_base_url="https://api.moonshot.cn/v1",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://api.moonshot.cn/v1/models",
        default_drop_params=True,
        thinking_channels=_DEEPSEEK_CHANNELS,
        supports_thinking=True,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
        regions=("cn", "intl"),
    ),
    "zai": ProviderCapability(
        provider_type="zai",
        litellm_prefix="zai/",
        default_base_url="https://api.z.ai/api/paas/v4",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://api.z.ai/api/paas/v4/models",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
        regions=("cn", "intl"),
    ),
    "volcengine": ProviderCapability(
        provider_type="volcengine",
        litellm_prefix="volcengine/",
        default_base_url="https://ark.cn-beijing.volces.com/api/v3",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://ark.cn-beijing.volces.com/api/v3/models",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "tencent": ProviderCapability(
        provider_type="tencent",
        litellm_prefix="tencent/",
        default_base_url="https://tokenhub-intl.tencentcloudmaas.com/v1",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint=("https://tokenhub-intl.tencentcloudmaas.com/v1/models"),
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "minimax": ProviderCapability(
        provider_type="minimax",
        litellm_prefix="minimax/",
        default_base_url="https://api.minimaxi.com",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://api.minimaxi.com/v1/models",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
        regions=("cn", "intl"),
    ),
    "ollama": ProviderCapability(
        provider_type="ollama",
        litellm_prefix="ollama/",
        default_base_url=None,
        requires_api_key=False,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="{base_url}/api/tags",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "qianfan": ProviderCapability(
        # 文心一言：litellm 1.97.0 已废弃 qianfan 前缀，走 openai/ 兼容组 + base_url。
        provider_type="qianfan",
        litellm_prefix="openai/",
        default_base_url="https://qianfan.baidubce.com/v2",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://qianfan.baidubce.com/v2/models",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "xfyun": ProviderCapability(
        # 讯飞星火：litellm 1.97.0 无 xfyun 前缀，走 openai/ 兼容组 + base_url。
        provider_type="xfyun",
        litellm_prefix="openai/",
        default_base_url="https://spark-api-open.xf-yun.com/v1",
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=False,
        discover_endpoint="https://spark-api-open.xf-yun.com/v1/models",
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
    "custom": ProviderCapability(
        provider_type="custom",
        litellm_prefix=None,
        default_base_url=None,
        requires_api_key=True,
        requires_api_version=False,
        requires_manual_model_entry=True,
        discover_endpoint=None,
        default_drop_params=True,
        thinking_channels=(),
        supports_thinking=False,
        default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
        default_max_retries=_DEFAULT_MAX_RETRIES,
    ),
}

#: 自定义回退 capability（未知厂商类型用，与 ``custom`` 同语义但 ``provider_type``
#: 字段保留调用方传入的原始值，便于排查日志中可见用户误填的类型名）。
_CUSTOM_FALLBACK: ProviderCapability = ProviderCapability(
    provider_type="custom",
    litellm_prefix=None,
    default_base_url=None,
    requires_api_key=True,
    requires_api_version=False,
    requires_manual_model_entry=True,
    discover_endpoint=None,
    default_drop_params=True,
    thinking_channels=(),
    supports_thinking=False,
    default_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
    default_max_retries=_DEFAULT_MAX_RETRIES,
)


def get_capability(provider_type: str) -> ProviderCapability:
    """按厂商类型查询能力元数据（未知类型回退 custom 语义）。

    参数:
        provider_type: 厂商类型字符串（注册表键）。

    返回:
        匹配的 ``ProviderCapability``；未匹配时返回 ``replace(_CUSTOM_FALLBACK,
        provider_type=provider_type)`` 构造的副本（与 ``custom`` 同语义，但
        ``provider_type`` 字段保留调用方传入的原始值，便于排查日志中可见用户
        误填的类型名；用 ``replace`` 构造新实例，避免污染 frozen 单例）。

    异常:
        无（永不抛——未知类型回退而非报错，由调用方决定如何提示用户）。

    副作用:
        无。
    """

    found = PROVIDER_CAPABILITIES.get(provider_type)
    if found is not None:
        return found
    # 未知类型：保留调用方传入的原始 provider_type 便于排查，不污染 frozen 单例。
    return replace(_CUSTOM_FALLBACK, provider_type=provider_type)


def get_provider_types() -> tuple[str, ...]:
    """返回全部已注册厂商类型键（用于派生 ``PROVIDER_TYPES`` 枚举与前端下拉）。

    参数:
        无。

    返回:
        注册表键的稳定元组（插入顺序，便于测试断言）。

    异常:
        无。

    副作用:
        无。
    """

    return tuple(PROVIDER_CAPABILITIES.keys())
