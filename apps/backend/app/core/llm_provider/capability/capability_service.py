"""OpenAI-compatible 模型能力查询。"""

from app.config.logging.logger import log
from app.core.llm_provider.capability.model_capability import ModelCapability


class CapabilityService:
    """读取静态模型能力，并把模型连接配置转换为运行时能力参数。"""

    @staticmethod
    def get_thinking_channel() -> str:
        """返回 OpenAI-compatible 响应中可选的 reasoning delta 字段名。"""

        return "reasoning_content"

    @staticmethod
    def get_vision_input_format() -> str:
        """返回 OpenAI-compatible 连接统一使用的视觉输入格式。"""

        return "openai_url"

    @staticmethod
    def get_model_context_window(model_name: str, *, context_window_k: int | None = None) -> int:
        """返回本次 Run 的上下文窗口 token 数。

        显式配置的 ``context_window_k`` 是连接配置事实，按千 token 转换；没有显式值时仅
        回退静态模型能力文件，供旧的轻量测试替身和未配置模型元数据场景使用。
        """

        if context_window_k is not None:
            return context_window_k * 1000
        return ModelCapability.get_capability(model_name).context_window

    @staticmethod
    def resolve_reasoning_effort(
        model_name: str,
        requested_effort: str | None,
    ) -> str | None:
        """把内部推理强度档位翻译成静态能力声明的原始档位。"""

        if requested_effort is None:
            return None
        effort = ModelCapability.get_capability(model_name).reasoning_effort
        if not effort.supported:
            return None
        resolved = effort.effort_map.get(requested_effort)
        if resolved is None:
            log.warning(
                "llm_reasoning_effort_unmapped",
                extra={
                    "msg": "推理强度档位无对应模型映射，跳过注入",
                    "data": {"model": model_name, "requested_effort": requested_effort},
                },
            )
        return resolved
