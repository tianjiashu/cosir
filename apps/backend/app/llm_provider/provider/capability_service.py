from app.config.logging.logger import log
from app.config.settings import Settings
from app.llm_provider.capability.model_capability import ModelCapability
from app.llm_provider.capability.provider_capability import ProviderCapability
from app.service.depends import get_provider_service


class CapabilityService:
    """
    模型与厂商能力查询服务。

    """

    def __init__(self):
        pass

    @staticmethod
    def get_thinking_channel(provider_id: int) -> str:
        provider = get_provider_service().get_provider(provider_id=provider_id)
        capability = ProviderCapability.get_capability(provider.name)
        return capability.thinking_channel

    @staticmethod
    def get_vision_input_format(provider_id: int) -> str:
        """按厂商取视觉输入格式（路由经验值，非模型事实）。

        DeepSeek 等 OpenAI 兼容厂商走 ``image_url`` 形式；其它厂商（Anthropic /
        Gemini）的视觉格式本期未实现，返回空串交由 workflow 转 ``VisionNotSupportedError``。

        参数:
            provider_id: 厂商记录主键（对应 ``providers`` 表的 ``id``）。

        返回:
            命中时返回视觉格式名（如 ``"openai_url"``）；未命中或 JSON 缺字段时返回空串。

        异常:
            无（缺配置不抛，由调用方决定兜底行为）。

        副作用:
            无（纯读 JSON）。
        """
        provider = get_provider_service().get_provider(provider_id=provider_id)
        capability = ProviderCapability.get_capability(provider.name)
        return capability.vision_input_format

    @staticmethod
    def get_model_context_window(model_name: str) -> int:
        """
        按模型名取最大上下文窗口（token），走完整解析链路。
        """
        model_max = ModelCapability.get_capability(model_name).context_window
        soft_cap = Settings.CONTEXT_WINDOW_TOKENS
        if soft_cap <= 0:
            return model_max
        return min(model_max, soft_cap)

    @staticmethod
    def resolve_reasoning_effort(
            model_name: str,
            requested_effort: str | None,
    ) -> str | None:
        """把内部推理强度档位翻译成厂商原始档位。

        内部档位（``low`` / ``high`` / ``max``）需经模型 ``effort_map`` 翻译成厂商原始档位
        （不同厂商档位取值不同，如 ``qwen-3.8-max`` 的 ``high``->``medium``、``max``->``xhigh``）
        再透传。未命中映射（模型声明支持推理但不支持该具体档位）返回 None，由调用方跳过注入。

        参数:
            model_name: 模型名（仅用于日志上下文，便于排查未命中）。
            requested_effort: 调用方请求的内部档位（已确认非 None）。
            effort_cap: 该模型的推理强度能力元数据（含 effort_map）。

        返回:
            翻译后的厂商原始档位字符串；未命中映射时返回 None。
        """
        if requested_effort is None:
            return None
        capability = ModelCapability.get_capability(model_name)
        effort = capability.reasoning_effort
        if not effort.supported:
            return None
        resolved = effort.effort_map.get(requested_effort, None)
        if resolved is None:
            log.warning(
                "llm_reasoning_effort_unmapped",
                extra={
                    "msg": "推理强度档位无对应厂商映射，跳过注入",
                    "data": {
                        "model": model_name,
                        "requested_effort": requested_effort,
                    },
                },
            )
        return resolved
