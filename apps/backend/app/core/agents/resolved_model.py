"""一次模型选型的最终解析结果：模型配置（连接与能力）+ Agent 偏好 + 本次 Run 的推理强度。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.models.conversation_run_options import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord


@dataclass(frozen=True)
class ResolvedModel:
    """模型构建（``build_chat_model``）的唯一输入；连接与能力字段全部非空，下游不再校验完整性。

    Attributes:
        model_config_id: 本次选用的模型配置；Agent Team 以它作为节点缺少独立模型配置时的回落值。
            只有连接测试里尚未保存的草稿配置为 ``None``。
        api_key: 只在进程内流动，不进 repr、日志或任何持久化。
        temperature … reasoning_effort: 请求偏好，``None`` 表示交给运行时默认值。
    """

    model_config_id: int | None
    base_url: str
    api_key: str = field(repr=False)
    model_name: str
    context_window_k: int
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_image: bool
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    drop_params: bool | None = None
    stream: bool | None = None
    reasoning_effort: ReasoningEffort | None = None

    def preferences(self) -> ModelSettings:
        """取回本次解析生效的请求偏好；Agent Team 节点沿用主 Agent 的模型时用它补默认值。"""
        return ModelSettings(
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            drop_params=self.drop_params,
            stream=self.stream,
            reasoning_effort=self.reasoning_effort,
        )


def resolve_model(
    config: ModelConfigRecord,
    preferences: ModelSettings | None = None,
    *,
    reasoning_effort: ReasoningEffort | None = None,
) -> ResolvedModel:
    """把模型配置与偏好解析为可直接构建模型的 ``ResolvedModel``。

    优先级从高到低：``reasoning_effort``（本次 Run 选择）> ``preferences``（Agent 偏好）；连接与
    能力字段只来自 ``config``，偏好不可能改写它们。

    异常:
        ModelSettingsError: 配置缺少 Base URL、``model_name`` 或有效的 ``context_window_k``；在
            装配期失败，而不是拖到首次模型请求。
    """

    if not config.base_url or not config.model_name or config.context_window_k <= 0:
        raise ModelSettingsError("模型连接配置缺少 base_url、model_name 或有效的 context_window_k")
    chosen = preferences or ModelSettings()
    return ResolvedModel(
        model_config_id=config.id,
        base_url=config.base_url,
        api_key=config.api_key,
        model_name=config.model_name,
        context_window_k=config.context_window_k,
        supports_thinking=config.supports_thinking,
        supports_reasoning_effort=config.supports_reasoning_effort,
        supports_image=config.supports_image,
        temperature=chosen.temperature,
        top_p=chosen.top_p,
        max_tokens=chosen.max_tokens,
        drop_params=chosen.drop_params,
        stream=chosen.stream,
        reasoning_effort=reasoning_effort or chosen.reasoning_effort,
    )
