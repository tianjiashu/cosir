"""``AgentRuntimeConfig.for_run`` 的合成契约：Agent 定义 + 本 Run 选中的模型配置 → 运行时配置。

只验证合成规则本身：连接与能力字段只来自模型配置、定义只补偏好、Run 推理强度最终覆盖、
不完整配置在装配期失败、定义不被修改。不覆盖 runner / workflow 链路。
"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_runtime_config import AgentRuntimeConfig
from app.core.agents.model_settings import ModelSettings, ModelSettingsError
from app.models.conversation_run_options import ConversationRunOptions
from app.models.model_config_record import ModelConfigRecord


def _definition(model_settings: ModelSettings | None = None) -> AgentProfile:
    return AgentProfile(
        agent_id="code-developer",
        role="developer",
        allowed_tools=["read_file", "write_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="Test child system prompt.",
        model_settings=model_settings or ModelSettings(),
    )


def _model_config(**overrides: object) -> ModelConfigRecord:
    fields: dict[str, object] = {
        "id": 3,
        "config_name": "run",
        "base_url": "https://run.test",
        "api_key": "run-key",
        "model_name": "run-model",
        "context_window_k": 64,
        "supports_thinking": True,
        "supports_reasoning_effort": True,
        "supports_image": True,
    }
    fields.update(overrides)
    return ModelConfigRecord(**fields)  # type: ignore[arg-type]


def _run(reasoning_effort: str | None = None) -> object:
    return SimpleNamespace(options=ConversationRunOptions(reasoning_effort=reasoning_effort))


def test_runtime_fields_come_only_from_model_config_and_definition_fills_preferences() -> None:
    definition = _definition(ModelSettings(temperature=0.2, top_p=0.5))

    config = AgentRuntimeConfig.for_run(definition, _run(), _model_config())  # type: ignore[arg-type]

    assert config.definition is definition
    assert config.model_config_id == 3
    settings = config.model_settings
    assert (settings.base_url, settings.api_key, settings.model_name) == (
        "https://run.test",
        "run-key",
        "run-model",
    )
    assert settings.context_window_k == 64 and settings.supports_image is True
    assert (settings.temperature, settings.top_p) == (0.2, 0.5)
    # 定义只是偏好来源，合成不回写它。
    assert definition.model_settings.model_name is None


def test_run_reasoning_effort_overrides_definition_preference() -> None:
    definition = _definition(ModelSettings(reasoning_effort="high"))

    config = AgentRuntimeConfig.for_run(definition, _run("low"), _model_config())  # type: ignore[arg-type]

    assert config.model_settings.reasoning_effort == "low"


def test_definition_preference_is_kept_without_run_override() -> None:
    definition = _definition(ModelSettings(reasoning_effort="high"))

    config = AgentRuntimeConfig.for_run(definition, _run(), _model_config())  # type: ignore[arg-type]

    assert config.model_settings.reasoning_effort == "high"


def test_incomplete_model_config_fails_at_assembly() -> None:
    """配置不完整在装配期就失败，而不是拖到首次模型请求。"""

    with pytest.raises(ModelSettingsError):
        AgentRuntimeConfig.for_run(  # type: ignore[arg-type]
            _definition(), _run(), _model_config(model_name="")
        )


def test_runtime_config_is_immutable() -> None:
    config = AgentRuntimeConfig.for_run(_definition(), _run(), _model_config())  # type: ignore[arg-type]

    with pytest.raises(FrozenInstanceError):
        config.model_config_id = 4  # type: ignore[misc]
