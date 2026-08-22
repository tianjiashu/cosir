"""LLM 运行时配置值对象（解析链的输出契约）。

单一职责：承载「一次模型调用的全部运行时配置」，由 service 层
``ModelResolverService`` 构造、被 core 层 ``factory.build_chat_model`` 消费。
放置在 ``models/``（业务值对象层）使 ``core → models`` 与 ``service → models``
均为合法依赖方向（见 ``docs/agent-model-provider-design.md`` §6.1）。

设计要点：
- **承载 ``api_key`` 明文（瞬时值）**：DB 是 Key 唯一事实来源，解析链在
  service 期已把明文透传进本对象；构建期 ``factory.build_chat_model`` 直接
  消费。明文只允许在本对象内部流动，禁止写入日志 / 事件 / API 响应
  （``ProviderRecord.to_dict`` 已刻意不输出 Key，本对象无公开序列化出口）。
- ``from_model_entry`` 为主路径（DB 是模型唯一事实来源，D16）：thinking /
  窗口 / Key 取自 provider + model 行（采样参数为运行时 Agent 覆盖，经
  ``from_values`` 注入）。
- ``from_profile`` 为 Agent 级覆盖路径（测试/自定义 profile 场景）：分层约束
  下禁止运行时 import ``core/agents`` / ``core/llm``，入参走鸭子类型；
  ``max_context_window`` 置 None，由 ``resolve_context_window`` 的
  ModelCatalog 兜底分支承担（语义等价，避免 ``models → core`` 反向依赖）。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models.provider_capability import get_capability

if TYPE_CHECKING:
    from app.models import ModelEntryRecord, ProviderRecord


@dataclass(frozen=True)
class LLMRuntimeConfig:
    """一次模型调用的运行时配置（litellm 路由名 + 接入与采样参数）。

    ``provider_type`` 承载接入维度信息：供 ``factory.build_chat_model`` 查
    ``ProviderCapability`` 决定参数透传与 thinking 注入。``thinking_channels``
    供 ``model_node`` 按厂商分派响应侧 thinking 抽取与回传剥离。
    """

    model_name: str
    base_url: str | None = None
    api_key: str | None = None
    max_context_window: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    # 厂商类型（``deepseek`` / ``azure`` / ``zai`` 等注册表键）；None 表示
    # 未解析到（由调用方回退 custom 语义）。
    provider_type: str | None = None
    # 响应侧 thinking 抽取通道元组（与 ``ProviderCapability.thinking_channels``
    # 对齐），供 ``model_node`` 多通道抽取与按 capability 的剥离判断。
    thinking_channels: tuple[str, ...] = ()
    # 请求侧 thinking 开启参数（None 表示不注入开启参数）。
    thinking_request: dict | None = None
    # 是否原样回传 thinking 块（含 Anthropic signature / Gemini thought /
    # OpenAI encrypted_content）。默认 True；排查问题时置 False 强制剥离不抛。
    thinking_roundtrip: bool = True
    # thinking 展示开关（``full`` / ``summary`` / ``off``），映射到 3.1 注入参数。
    thinking_display: str = "summary"

    @classmethod
    def from_values(
        cls,
        model_name: str,
        settings: object | None = None,
    ) -> "LLMRuntimeConfig":
        """从裸模型名 + ``ModelSettings`` 形态折叠构造（兼容路径统一入口）。

        供两处复用：``factory.build_chat_model`` 的旧签名入口折叠、
        ``from_profile`` 的 profile 字段读取。``settings`` 走鸭子类型只读
        ``temperature`` / ``top_p`` / ``max_tokens`` / ``thinking`` /
        ``base_url`` / ``api_key`` 字段（避免运行时 import core 类型）。

        参数:
            model_name: litellm 路由名（带 provider 前缀）。
            settings: 可选 ``ModelSettings``（或同字段形态对象）；None 时全部
                覆盖项为空。

        返回:
            折叠后的 ``LLMRuntimeConfig``（``max_context_window`` 为 None，
            窗口兜底交 ``resolve_context_window`` 的 ModelCatalog 分支）。

        异常:
            无。

        副作用:
            无。
        """

        if settings is None:
            return cls(model_name=model_name)
        return cls(
            model_name=model_name,
            base_url=getattr(settings, "base_url", None),
            api_key=getattr(settings, "api_key", None),
            max_context_window=None,
            temperature=getattr(settings, "temperature", None),
            top_p=getattr(settings, "top_p", None),
            max_tokens=getattr(settings, "max_tokens", None),
            thinking=getattr(settings, "thinking", None),
        )

    @classmethod
    def from_profile(cls, profile: object) -> "LLMRuntimeConfig":
        """从 Agent profile 构造（Agent 级覆盖路径，测试/自定义 profile 场景）。

        运行时只读 ``profile.model_name`` / ``profile.model_settings`` 字段
        （鸭子类型，TYPE_CHECKING 不引入 core 类型）；主路径是
        ``from_model_entry``（DB 行为唯一事实源，D16）。

        参数:
            profile: ``AgentProfile`` 或同字段形态对象。

        返回:
            对应的 ``LLMRuntimeConfig``。

        异常:
            无。

        副作用:
            无。
        """

        return cls.from_values(
            model_name=getattr(profile, "model_name"),
            settings=getattr(profile, "model_settings", None),
        )

    @classmethod
    def from_model_entry(
        cls,
        provider: "ProviderRecord",
        model_entry: "ModelEntryRecord",
    ) -> "LLMRuntimeConfig":
        """从 DB 的 provider + model 行构造（主路径）。

        thinking / 窗口 / Key 取自 DB 行（D16：DB 是唯一事实源）；
        ``max_context_window`` 来自 ``models.max_context_window``（NOT NULL，
        D15），保证 ``resolve_context_window`` 的 ``db_window`` 永不落空。
        Key 明文自 provider 行透传，仅在此对象内部流动，不进日志 / 事件。

        ``provider_type`` 直接取自 provider 行；
        ``thinking_channels`` 经 ``get_capability(provider.provider_type)``
        从注册表解析（未知类型回退 custom 语义，通道为空），供
        ``model_node`` 按厂商分派抽取与剥离。

        参数:
            provider: 模型归属厂商的 ``ProviderRecord``（提供 base_url /
                api_key / provider_type）。
            model_entry: 模型行 ``ModelEntryRecord``（提供窗口与 thinking 标识）。

        返回:
            组装后的 ``LLMRuntimeConfig``。

        异常:
            无（``get_capability`` 对未知类型回退而非抛错）。

        副作用:
            无。
        """

        capability = get_capability(provider.provider_type)
        return cls(
            model_name=model_entry.model_name,
            base_url=provider.base_url,
            api_key=provider.api_key,
            max_context_window=model_entry.max_context_window,
            thinking=True if model_entry.supports_thinking else None,
            provider_type=provider.provider_type,
            thinking_channels=capability.thinking_channels,
        )
