"""一次模型选型的最终解析结果：模型配置（连接与能力）+ Agent 偏好 + 本次 Run 的推理强度。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.agents.model_settings import ModelSettings
from app.models.run_options import ReasoningEffort
from app.models.model_config_record import ModelConfigRecord


@dataclass(frozen=True)
class ResolvedModel:
    """模型构建（``build_chat_model``）的唯一输入；连接与能力字段全部非空，下游不再校验完整性。

    Attributes:
        model_config_id: 本次选用的模型（「连接 id/模型名」）。
        api_key: 只在进程内流动，不进 repr、日志或任何持久化。
        temperature … reasoning_effort: 请求偏好，``None`` 表示交给运行时默认值。
    """

    model_config_id: str
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


def resolve_model(
    config: ModelConfigRecord,
    preferences: ModelSettings | None = None,
    *,
    reasoning_effort: ReasoningEffort | None = None,
) -> ResolvedModel:
    """把模型配置与偏好解析为可直接构建模型的 ``ResolvedModel``。

    优先级从高到低：``reasoning_effort``（本次 Run 选择）> ``preferences``（Agent 偏好）；连接与
    能力字段只来自 ``config``（地址与 Key 取自它所属的连接），偏好不可能改写它们。
    连接与模型字段的完整性由模型配置文件的契约在读取边界保证，这里不再校验。
    """

    connection, model = config.connection, config.model
    chosen = preferences or ModelSettings()
    return ResolvedModel(
        model_config_id=config.id,
        base_url=connection.base_url,
        api_key=connection.api_key,
        model_name=model.model_name,
        context_window_k=model.context_window_k,
        supports_thinking=model.supports_thinking,
        supports_reasoning_effort=model.supports_reasoning_effort,
        supports_image=model.supports_image,
        temperature=chosen.temperature,
        top_p=chosen.top_p,
        max_tokens=chosen.max_tokens,
        drop_params=chosen.drop_params,
        stream=chosen.stream,
        reasoning_effort=reasoning_effort or chosen.reasoning_effort,
    )
